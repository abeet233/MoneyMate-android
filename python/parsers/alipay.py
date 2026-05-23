import csv
import re
from datetime import datetime

def detect_encoding(filepath):
    for enc in ['gbk', 'utf-8', 'gb2312', 'gb18030']:
        try:
            with open(filepath, 'r', encoding=enc) as f:
                f.read(1024)
            return enc
        except (UnicodeDecodeError, UnicodeError):
            continue
    return 'gbk'

def parse_alipay_bill(filepath):
    encoding = detect_encoding(filepath)

    with open(filepath, 'r', encoding=encoding) as f:
        reader = csv.reader(f)
        lines = list(reader)

    header = None
    header_row = 0
    for i, row in enumerate(lines):
        non_empty = [c for c in row if c.strip()]
        if len(non_empty) >= 8 and any('交易时间' in c for c in row):
            header_row = i
            header = [c.strip() for c in row]
            break

    if not header:
        return [], '无法定位表头'

    col_map = {}
    for idx, name in enumerate(header):
        if '交易时间' in name:
            col_map['time'] = idx
        elif '交易分类' in name:
            col_map['category'] = idx
        elif '交易对方' in name:
            col_map['counterparty'] = idx
        elif '商品说明' in name:
            col_map['item'] = idx
        elif '收/支' in name:
            col_map['direction'] = idx
        elif '金额' in name:
            col_map['amount'] = idx
        elif '收/支方式' in name:
            col_map['method'] = idx
        elif '交易状态' in name:
            col_map['status'] = idx
        elif '交易订单号' in name:
            col_map['external_id'] = idx
        elif '商家订单号' in name:
            col_map['merchant_id'] = idx
        elif '备注' in name:
            col_map['note'] = idx

    required = ['time', 'direction', 'amount', 'external_id']
    missing = [r for r in required if r not in col_map]
    if missing:
        return [], f'缺少必要列: {missing}'

    transactions = []
    for row in lines[header_row+1:]:
        if len(row) < max(col_map.values()) + 1:
            continue

        external_id = row[col_map['external_id']].strip()
        if not external_id:
            continue

        time_str = row[col_map['time']].strip()
        try:
            occurred_at = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            continue

        direction_raw = row[col_map['direction']].strip()
        category_raw = row[col_map['category']].strip() if 'category' in col_map else ''
        item_desc = row[col_map['item']].strip() if 'item' in col_map and len(row) > col_map['item'] else ''

        direction, flow_type = resolve_alipay_direction(direction_raw, category_raw, item_desc)

        amount_str = row[col_map['amount']].strip()
        try:
            amount = float(amount_str)
        except ValueError:
            amount = 0

        counterparty = row[col_map['counterparty']].strip() if 'counterparty' in col_map and len(row) > col_map['counterparty'] else ''
        payment_method = row[col_map['method']].strip() if 'method' in col_map and len(row) > col_map['method'] else ''
        status = row[col_map['status']].strip() if 'status' in col_map and len(row) > col_map['status'] else ''
        merchant_id = row[col_map['merchant_id']].strip() if 'merchant_id' in col_map and len(row) > col_map['merchant_id'] else ''
        note = row[col_map['note']].strip() if 'note' in col_map and len(row) > col_map['note'] else ''

        if flow_type == 'unknown':
            flow_type = infer_flow_type_alipay(category_raw)

        transactions.append({
            'occurred_at': occurred_at,
            'direction': direction,
            'amount': amount,
            'channel': 'alipay',
            'counterparty': counterparty,
            'item_desc': item_desc,
            'payment_method': payment_method,
            'status': status,
            'external_id': external_id,
            'merchant_id': merchant_id,
            'flow_type': flow_type,
            'note': note,
        })

    return transactions, None

def resolve_alipay_direction(direction_raw, category, item_desc):
    d = direction_raw.strip()
    c = (category or '').strip()
    item = (item_desc or '').strip()

    if any(k in d for k in ['退款', '收款', '到账', '收入']) or c == '退款' or item.startswith('退款-'):
        return 'income', 'income'
    if any(k in d for k in ['支出', '付款', '扣款', '消费']):
        return 'expense', infer_category_flow(c)

    # 不计收支 - secondary classification
    if category in ('投资理财', '信用借还'):
        return 'neutral', 'transfer'
    return 'neutral', 'transfer'

ALIPAY_CATEGORY_MAP = {
    '餐饮美食': 'food',
    '交通出行': 'transport',
    '日用百货': 'shopping',
    '服饰装扮': 'clothing',
    '家居装修': 'home',
    '文化休闲': 'entertainment',
    '商业服务': 'service',
    '充值缴费': 'utilities',
    '爱心捐赠': 'donation',
    '投资理财': 'investment',
    '信用借还': 'credit',
    '退款': 'refund',
    '收入': 'income',
}

def infer_category_flow(category):
    return ALIPAY_CATEGORY_MAP.get(category, 'unknown')

def infer_flow_type_alipay(category):
    return ALIPAY_CATEGORY_MAP.get(category, 'unknown')
