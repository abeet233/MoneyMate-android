import os
import uuid
import hashlib
import json
from datetime import datetime, timedelta

from fastapi import FastAPI, UploadFile, File, Form, Query, HTTPException
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from database import get_db, init_db, now_cst, CST, get_data_dir
from parsers import parse_wechat_bill, parse_alipay_bill

app = FastAPI(title="MoneyMate")

init_db()

FRONTEND_DIR = os.path.dirname(os.path.abspath(__file__))  # index.html is alongside main.py
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# --------------- Events ---------------

@app.post("/api/events")
async def receive_event(data: dict):
    text = data.get('text', '')
    app_name = data.get('app', 'wechat')
    timestamp_str = data.get('timestamp', '')
    title = data.get('title', '')

    # parse amount
    import re
    amounts = re.findall(r'(\d+\.?\d*)', text)
    amount = float(amounts[0]) if amounts else None

    # parse direction
    if any(k in text for k in ['支付', '付款', '扣款', '消费']):
        direction = 'expense'
    elif any(k in text for k in ['收款', '退款', '到账', '转入']):
        direction = 'income'
    else:
        direction = 'expense'

    # parse time
    try:
        occurred_at = datetime.fromisoformat(timestamp_str)
    except (ValueError, TypeError):
        occurred_at = datetime.now(CST)

    combined_text = f"{title} {text}".strip()

    raw_amount = data.get('amount')
    try:
        amount = float(raw_amount) if raw_amount not in (None, '') else None
    except (TypeError, ValueError):
        amount = None

    raw_direction = data.get('direction', '')

    if amount is None:
        amounts = re.findall(r'(?:[¥￥]\s*(\d+\.?\d{0,2})|(\d+\.?\d{0,2})\s*元)', combined_text)
        flat_amounts = [a or b for a, b in amounts if (a or b)]
        amount = float(flat_amounts[0]) if flat_amounts else None

    if raw_direction in ('expense', 'income'):
        direction = raw_direction
    else:
        if any(k in combined_text for k in ['退款', '收款', '到账', '转入']):
            direction = 'income'
        elif any(k in combined_text for k in ['支付', '付款', '扣款', '消费', '支出']):
            direction = 'expense'
        else:
            direction = 'expense'

    # generate temporary external_id
    text_hash = hashlib.md5(combined_text.encode()).hexdigest()[:8]
    external_id = f"{app_name}_{occurred_at.strftime('%Y%m%d%H%M%S')}_{amount}_{text_hash}"

    conn = get_db()
    now = now_cst()
    transaction_id = str(uuid.uuid4())

    # dedup - check if similar notification already exists
    existing = conn.execute(
        "SELECT id FROM transactions WHERE external_id = ?", (external_id,)
    ).fetchone()
    if existing:
        conn.close()
        return {'id': existing['id'], 'match': False, 'duplicate': True}

    # dedup - check for same amount + channel within 30 seconds
    if amount is not None:
        oc_str = occurred_at.isoformat()
        w0 = (occurred_at - timedelta(seconds=30)).isoformat()
        w1 = (occurred_at + timedelta(seconds=30)).isoformat()
        dup = conn.execute(
            "SELECT id, source FROM transactions WHERE channel = ? AND amount = ? AND occurred_at >= ? AND occurred_at <= ? LIMIT 1",
            (app_name, amount, w0, w1)
        ).fetchone()
        if dup:
            if dup['source'] == 'notification':
                conn.execute(
                    "UPDATE transactions SET review_status = 'pending' WHERE id = ?",
                    (dup['id'],)
                )
                conn.commit()
                conn.close()
                return {'id': dup['id'], 'match': False, 'duplicate': False}

    conn.execute("""
        INSERT INTO transactions (id, occurred_at, recorded_at, direction, amount, channel,
            counterparty, item_desc, payment_method, status, external_id,
            flow_type, source, review_status, raw_text, notes)
        VALUES (?, ?, ?, ?, ?, ?, '', ?, '', '', ?, 'unknown', 'notification', 'pending', ?, '')
    """, (
        transaction_id,
        occurred_at.isoformat(),
        now,
        direction,
        amount,
        app_name,
        title,
        external_id,
        text,
    ))
    conn.commit()
    conn.close()
    return {'id': transaction_id, 'match': False, 'duplicate': False}

# --------------- Transactions ---------------

@app.get("/api/transactions")
def list_transactions(
    year: int = None, month: int = None,
    direction: str = None, channel: str = None,
    flow_type: str = None,
    pending: bool = False,
    limit: int = 200, offset: int = 0
):
    conn = get_db()
    conditions = []
    params = []

    if year and month:
        start = f"{year}-{month:02d}-01T00:00:00"
        if month == 12:
            end = f"{year+1}-01-01T00:00:00"
        else:
            end = f"{year}-{month+1:02d}-01T00:00:00"
        conditions.append("occurred_at >= ? AND occurred_at < ?")
        params.extend([start, end])

    if direction:
        conditions.append("direction = ?")
        params.append(direction)

    if channel:
        conditions.append("channel = ?")
        params.append(channel)

    if flow_type:
        conditions.append("flow_type = ?")
        params.append(flow_type)

    if pending:
        conditions.append("review_status = 'pending'")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"SELECT * FROM transactions {where} ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
        params + [limit, offset]
    ).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        d['pending'] = d.get('review_status') == 'pending'
        result.append(d)

    total = conn.execute(
        f"SELECT COUNT(*) as cnt FROM transactions {where}", params
    ).fetchone()['cnt']

    conn.close()
    return {'items': result, 'total': total}

@app.put("/api/transactions/{tid}")
async def update_transaction(tid: str, data: dict):
    conn = get_db()
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (tid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Not found")

    if data.get('review_status') == 'confirmed' and row['review_status'] == 'pending':
        window_start = (datetime.fromisoformat(row['occurred_at']) - timedelta(minutes=2)).isoformat()
        window_end = (datetime.fromisoformat(row['occurred_at']) + timedelta(minutes=2)).isoformat()
        matched = conn.execute(
            "SELECT id FROM transactions WHERE id != ? AND review_status != 'pending' AND channel = ? AND amount = ? AND direction = ? AND occurred_at >= ? AND occurred_at <= ? LIMIT 1",
            (tid, row['channel'], row['amount'], row['direction'], window_start, window_end)
        ).fetchone()
        if matched:
            conn.execute("DELETE FROM transactions WHERE id = ?", (tid,))
            conn.commit()
            conn.close()
            return {'ok': True, 'merged': True, 'matched_id': matched['id']}

    allowed = ['direction', 'amount', 'counterparty', 'item_desc', 'payment_method',
               'status', 'flow_type', 'notes', 'review_status']
    sets = []
    params = []
    for k in allowed:
        if k in data:
            sets.append(f"{k} = ?")
            params.append(data[k])
    if not sets:
        conn.close()
        raise HTTPException(400, "No valid fields")
    params.append(tid)
    conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    conn.close()
    return {'ok': True}

@app.delete("/api/transactions/{tid}")
def delete_transaction(tid: str):
    conn = get_db()
    row = conn.execute("SELECT external_id FROM transactions WHERE id = ?", (tid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Not found")
    conn.execute("INSERT OR IGNORE INTO deleted_ids (external_id, deleted_at) VALUES (?, ?)",
                 (row['external_id'], now_cst()))
    conn.execute("DELETE FROM transactions WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    return {'ok': True}

@app.post("/api/transactions/confirm-pending")
def confirm_pending_transactions():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM transactions WHERE review_status = 'pending' ORDER BY occurred_at DESC"
    ).fetchall()

    confirmed = 0
    merged = 0
    for r in rows:
        amount = r['amount']
        matched = None
        if amount is not None:
            try:
                occurred = datetime.fromisoformat(r['occurred_at'])
                window_start = (occurred - timedelta(minutes=2)).isoformat()
                window_end = (occurred + timedelta(minutes=2)).isoformat()
                matched = conn.execute(
                    "SELECT id FROM transactions WHERE id != ? AND review_status != 'pending' AND channel = ? AND amount = ? AND direction = ? AND occurred_at >= ? AND occurred_at <= ? LIMIT 1",
                    (r['id'], r['channel'], amount, r['direction'], window_start, window_end)
                ).fetchone()
            except (ValueError, TypeError):
                matched = None

        if matched:
            conn.execute("DELETE FROM transactions WHERE id = ?", (r['id'],))
            merged += 1
        else:
            conn.execute("UPDATE transactions SET review_status = 'confirmed' WHERE id = ?", (r['id'],))
            confirmed += 1

    conn.commit()
    pending_after = conn.execute(
        "SELECT COUNT(*) as cnt FROM transactions WHERE review_status = 'pending'"
    ).fetchone()['cnt']
    conn.close()
    return {'ok': True, 'confirmed': confirmed, 'merged': merged, 'pending_after': pending_after}

# --------------- Bill Import ---------------

def detect_channel_from_file(filepath, filename):
    lower = filename.lower()
    if lower.endswith('.xlsx') or lower.endswith('.xls'):
        return 'wechat'
    if any(lower.endswith(ext) for ext in ['.csv', '.txt', '.tsv']):
        with open(filepath, 'rb') as f:
            raw = f.read(65536)
        for enc in ['gbk', 'utf-8', 'gb2312', 'gb18030']:
            try:
                text = raw.decode(enc)
                if '支付宝' in text or any(k in text for k in ['交易时间', '交易对方', '交易订单号', '商家订单号', '收/支方式']):
                    return 'alipay'
            except (UnicodeError, UnicodeDecodeError):
                continue
        return 'alipay'
    with open(filepath, 'rb') as f:
        raw = f.read(65536)
    for enc in ['gbk', 'utf-8', 'gb2312', 'gb18030']:
        try:
            text = raw.decode(enc)
            if '支付宝' in text or ('交易时间' in text and '交易对方' in text):
                return 'alipay'
        except (UnicodeError, UnicodeDecodeError):
            continue
    return None

@app.post("/api/import/bill")
async def import_bill(file: UploadFile = File(...)):
    upload_dir = os.path.join(get_data_dir(), "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    filename = os.path.basename(file.filename or f"bill_{uuid.uuid4()}")
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(await file.read())

    channel = detect_channel_from_file(filepath, file.filename)
    if not channel:
        return JSONResponse({'error': '无法识别账单类型，请确认文件为微信xlsx或支付宝csv'}, status_code=400)

    if channel == 'wechat':
        transactions, err = parse_wechat_bill(filepath)
    else:
        transactions, err = parse_alipay_bill(filepath)

    if err:
        return JSONResponse({'error': err}, status_code=400)

    conn = get_db()
    now = now_cst()
    matched = 0
    new = 0
    skipped = 0
    conflict_detail = []

    for txn in transactions:
        ext_id = txn['external_id']

        # check tombstone
        deleted = conn.execute("SELECT 1 FROM deleted_ids WHERE external_id = ?", (ext_id,)).fetchone()
        if deleted:
            skipped += 1
            continue

        occurred = txn['occurred_at']
        if isinstance(occurred, datetime):
            occurred_str = occurred.isoformat()
        else:
            occurred_str = str(occurred)

        # exact match by external_id
        existing = conn.execute("SELECT id, source FROM transactions WHERE external_id = ?", (ext_id,)).fetchone()

        if existing:
            conn.execute("""
                UPDATE transactions SET
                    occurred_at = ?, direction = ?, amount = ?, counterparty = ?,
                    item_desc = ?, payment_method = ?, status = ?, flow_type = ?, merchant_id = ?, review_status = 'confirmed'
                WHERE id = ?
            """, (
                occurred_str, txn['direction'], txn['amount'], txn['counterparty'],
                txn['item_desc'], txn['payment_method'], txn['status'], txn['flow_type'],
                txn['merchant_id'], existing['id']
            ))
            matched += 1
        else:
            # fuzzy match for notification-sourced records
            occurred_dt = occurred if isinstance(occurred, datetime) else datetime.fromisoformat(occurred_str)
            window_start = (occurred_dt - timedelta(minutes=1)).isoformat()
            window_end = (occurred_dt + timedelta(minutes=1)).isoformat()

            fuzzy_match = conn.execute("""
                SELECT id FROM transactions
                WHERE channel = ? AND amount = ? AND source = 'notification'
                  AND occurred_at >= ? AND occurred_at <= ?
                LIMIT 1
            """, (txn['channel'], txn['amount'], window_start, window_end)).fetchone()

            if fuzzy_match:
                conn.execute("""
                    UPDATE transactions SET
                        occurred_at = ?, external_id = ?, direction = ?, amount = ?,
                        counterparty = ?, item_desc = ?, payment_method = ?, status = ?,
                        flow_type = ?, merchant_id = ?, source = 'bill_import', review_status = 'confirmed'
                    WHERE id = ?
                """, (
                    occurred_str, ext_id, txn['direction'], txn['amount'],
                    txn['counterparty'], txn['item_desc'], txn['payment_method'], txn['status'],
                    txn['flow_type'], txn['merchant_id'], fuzzy_match['id']
                ))
                matched += 1
            else:
                tid = str(uuid.uuid4())
                conn.execute("""
                    INSERT INTO transactions (id, occurred_at, recorded_at, direction, amount,
                        channel, counterparty, item_desc, payment_method, status, external_id,
                        flow_type, source, review_status, merchant_id, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'bill_import', 'confirmed', ?, ?)
                """, (
                    tid, occurred_str, now, txn['direction'], txn['amount'],
                    txn['channel'], txn['counterparty'], txn['item_desc'],
                    txn['payment_method'], txn['status'], ext_id,
                    txn['flow_type'], txn['merchant_id'], txn.get('note', '')
                ))
                new += 1

    if transactions:
        all_times = []
        for txn in transactions:
            ot = txn['occurred_at']
            if isinstance(ot, datetime):
                all_times.append(ot)
            else:
                try:
                    all_times.append(datetime.fromisoformat(str(ot)))
                except (ValueError, TypeError):
                    pass
        if all_times:
            min_time = min(all_times).isoformat()
            max_time = max(all_times).isoformat()

            orphan_notifications = conn.execute("""
                SELECT COUNT(*) as cnt FROM transactions
                WHERE review_status = 'pending'
                  AND occurred_at >= ? AND occurred_at <= ?
            """, (min_time, max_time)).fetchone()['cnt']

            if orphan_notifications > 0:
                conflict_detail.append(f"通知记录 {orphan_notifications} 条在账单时间范围内未被匹配，已标记为待确认")

    log_id = str(uuid.uuid4())
    detail = f"匹配 {matched} 条，新增 {new} 条，跳过 {skipped} 条"
    if conflict_detail:
        detail += "；" + "；".join(conflict_detail)
    conn.execute("INSERT INTO reconciliation_log (id, created_at, type, detail) VALUES (?, ?, 'import', ?)",
                 (log_id, now, detail))
    conn.commit()
    conn.close()

    os.remove(filepath)
    return {'matched': matched, 'new': new, 'skipped': skipped, 'conflicts': orphan_notifications if transactions else 0}

@app.post("/api/import/bill-json")
async def import_bill_json(data: dict):
    """Mobile-friendly: accept {file: base64, filename: 'xxx.csv'}"""
    import base64
    file_b64 = data.get('file', '')
    filename = data.get('filename', 'bill.csv')
    if not file_b64:
        return JSONResponse({'error': '未收到文件数据'}, status_code=400)
    try:
        file_bytes = base64.b64decode(file_b64)
    except Exception:
        return JSONResponse({'error': '文件解码失败'}, status_code=400)

    upload_dir = os.path.join(get_data_dir(), "uploads")
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(file_bytes)

    channel = detect_channel_from_file(filepath, filename)
    if not channel:
        os.remove(filepath)
        return JSONResponse({'error': '无法识别账单类型'}, status_code=400)

    if channel == 'wechat':
        transactions, err = parse_wechat_bill(filepath)
    else:
        transactions, err = parse_alipay_bill(filepath)

    if err:
        os.remove(filepath)
        return JSONResponse({'error': err}, status_code=400)

    conn = get_db()
    now = now_cst()
    matched = 0; new = 0; skipped = 0
    for txn in transactions:
        ext_id = txn['external_id']
        deleted = conn.execute("SELECT 1 FROM deleted_ids WHERE external_id = ?", (ext_id,)).fetchone()
        if deleted: skipped += 1; continue
        occurred = txn['occurred_at']
        occurred_str = occurred.isoformat() if isinstance(occurred, datetime) else str(occurred)
        existing = conn.execute("SELECT id FROM transactions WHERE external_id = ?", (ext_id,)).fetchone()
        if existing:
            conn.execute("UPDATE transactions SET occurred_at=?,direction=?,amount=?,counterparty=?,item_desc=?,payment_method=?,status=?,flow_type=?,merchant_id=?,review_status='confirmed' WHERE id=?",
                (occurred_str, txn['direction'], txn['amount'], txn['counterparty'], txn['item_desc'], txn['payment_method'], txn['status'], txn['flow_type'], txn['merchant_id'], existing['id']))
            matched += 1
        else:
            occurred_dt = occurred if isinstance(occurred, datetime) else datetime.fromisoformat(occurred_str)
            window_start = (occurred_dt - timedelta(minutes=1)).isoformat()
            window_end = (occurred_dt + timedelta(minutes=1)).isoformat()
            fuzzy = conn.execute("SELECT id FROM transactions WHERE channel=? AND amount=? AND source='notification' AND occurred_at>=? AND occurred_at<=? LIMIT 1",
                (txn['channel'], txn['amount'], window_start, window_end)).fetchone()
            if fuzzy:
                conn.execute("UPDATE transactions SET occurred_at=?,external_id=?,direction=?,amount=?,counterparty=?,item_desc=?,payment_method=?,status=?,flow_type=?,merchant_id=?,source='bill_import',review_status='confirmed' WHERE id=?",
                    (occurred_str, ext_id, txn['direction'], txn['amount'], txn['counterparty'], txn['item_desc'], txn['payment_method'], txn['status'], txn['flow_type'], txn['merchant_id'], fuzzy['id']))
                matched += 1
            else:
                tid = str(uuid.uuid4())
                conn.execute("INSERT INTO transactions (id,occurred_at,recorded_at,direction,amount,channel,counterparty,item_desc,payment_method,status,external_id,flow_type,source,review_status,merchant_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tid, occurred_str, now, txn['direction'], txn['amount'], txn['channel'], txn['counterparty'], txn['item_desc'], txn['payment_method'], txn['status'], ext_id, txn['flow_type'], 'bill_import', 'confirmed', txn['merchant_id']))
                new += 1
    conn.commit()
    conn.close()
    os.remove(filepath)
    return {'matched': matched, 'new': new, 'skipped': skipped, 'conflicts': 0}

# --------------- Buckets ---------------

def eval_formula(formula, bucket_values):
    if not formula:
        return bucket_values.get('__self__', 0)
    expr = formula
    for k, v in bucket_values.items():
        expr = expr.replace(k, str(v))
    try:
        return float(eval(expr))
    except Exception:
        return 0

def recalc_buckets(conn):
    buckets = {r['key']: dict(r) for r in conn.execute("SELECT * FROM buckets").fetchall()}

    # topological sort by dependency
    dep_order = []
    visited = set()
    def visit(key, path):
        if key in path:
            return  # cycle detected, skip
        if key in visited:
            return
        b = buckets.get(key)
        if not b:
            return
        if b['source'] == 'formula' and b['formula']:
            for other_key in buckets:
                if other_key != key and other_key in (b['formula'] or ''):
                    visit(other_key, path + [key])
        visited.add(key)
        dep_order.append(key)

    for key in buckets:
        visit(key, [])

    now = now_cst()
    bucket_values = {}

    # first pass: manual values
    for key, b in buckets.items():
        if b['source'] == 'manual':
            bucket_values[key] = b['value']

    # second pass: formula values in dependency order
    for key in dep_order:
        b = buckets[key]
        if b['source'] == 'formula':
            # check if this bucket has children (parent-child hierarchy)
            children = [k for k, v in buckets.items() if v.get('parent') == key]
            if children:
                val = sum(buckets[k]['value'] for k in children)
            elif b['formula']:
                val = eval_formula(b['formula'], {**bucket_values, '__self__': b['value']})
            else:
                val = b['value']
            bucket_values[key] = val
            conn.execute("UPDATE buckets SET value = ?, updated_at = ? WHERE key = ?", (val, now, key))

    conn.commit()
    return bucket_values

def recalc_buckets_readonly(conn):
    """Compute formula bucket values without writing to DB."""
    buckets = {r['key']: dict(r) for r in conn.execute("SELECT * FROM buckets").fetchall()}
    bucket_values = {}

    for key, b in buckets.items():
        if b['source'] == 'manual':
            bucket_values[key] = b['value']

    for key, b in buckets.items():
        if b['source'] == 'formula':
            children = [k for k, v in buckets.items() if v.get('parent') == key]
            if key == 'investment_change':
                records = conn.execute("SELECT SUM(CASE WHEN direction='profit' THEN amount ELSE -amount END) as total FROM investment_records").fetchone()
                val = (records['total'] or 0) + b['value']  # value = initial offset
            elif children:
                val = sum(buckets[k]['value'] for k in children)
            elif b['formula']:
                val = eval_formula(b['formula'], {**bucket_values, '__self__': b['value']})
            else:
                val = b['value']
            bucket_values[key] = val

    return bucket_values

@app.get("/api/buckets")
def get_buckets():
    conn = get_db()
    vals = recalc_buckets_readonly(conn)
    rows = conn.execute("SELECT * FROM buckets ORDER BY key").fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d['value'] = vals.get(r['key'], r['value'])
        result.append(d)
    conn.close()
    return result

@app.put("/api/buckets/{key}")
async def update_bucket(key: str, data: dict):
    conn = get_db()
    existing = conn.execute("SELECT * FROM buckets WHERE key = ?", (key,)).fetchone()
    if not existing:
        conn.execute("INSERT INTO buckets (key, label, value, source, formula, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (key, data.get('label', key), data.get('value', 0),
                      data.get('source', 'manual'), data.get('formula'), now_cst()))
    else:
        if 'value' in data:
            conn.execute("UPDATE buckets SET value = ?, updated_at = ? WHERE key = ?",
                         (data['value'], now_cst(), key))
        if 'source' in data:
            conn.execute("UPDATE buckets SET source = ? WHERE key = ?", (data['source'], key))
        if 'formula' in data:
            conn.execute("UPDATE buckets SET formula = ? WHERE key = ?", (data['formula'], key))
        if 'parent' in data:
            conn.execute("UPDATE buckets SET parent = ? WHERE key = ?", (data['parent'], key))
        if 'label' in data:
            conn.execute("UPDATE buckets SET label = ? WHERE key = ?", (data['label'], key))

    conn.execute("INSERT INTO bucket_history (id, bucket_key, value, recorded_at) VALUES (?, ?, ?, ?)",
                 (str(uuid.uuid4()), key,
                  data.get('value', existing['value'] if existing else 0), now_cst()))
    conn.commit()
    conn.close()
    return {'ok': True}

@app.post("/api/buckets")
async def create_bucket(data: dict):
    conn = get_db()
    key = data['key']
    conn.execute("INSERT INTO buckets (key, label, value, source, formula, parent, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (key, data.get('label', key), data.get('value', 0),
                  data.get('source', 'manual'), data.get('formula'), data.get('parent'), now_cst()))
    conn.commit()
    conn.close()
    return {'ok': True}

@app.delete("/api/buckets/{key}")
def delete_bucket(key: str):
    core = {'total_asset', 'investment_value', 'life_value', 'total_liability', 'net_worth', 'investment_change'}
    if key in core:
        raise HTTPException(400, "核心科目不可删除")

    conn = get_db()
    row = conn.execute("SELECT value, parent FROM buckets WHERE key = ?", (key,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "科目不存在")

    if row['value'] and row['value'] != 0:
        conn.close()
        parent_info = f"，归属于「{row['parent']}」" if row['parent'] else ''
        raise HTTPException(400,
            f"「{key}」当前余额为 ¥{row['value']:.2f}{parent_info}。请先将余额清零或结转至其他科目后再删除。")

    # cascade: clear references
    conn.execute("DELETE FROM bucket_history WHERE bucket_key = ?", (key,))
    conn.execute("DELETE FROM buckets WHERE key = ?", (key,))
    conn.commit()
    conn.close()
    return {'ok': True}

# --------------- Settings ---------------

@app.get("/api/settings")
def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT * FROM settings").fetchall()
    conn.close()
    return {r['key']: r['value'] for r in rows}

@app.put("/api/settings/{key}")
async def update_setting(key: str, data: dict):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                 (key, str(data['value'])))
    conn.commit()
    conn.close()
    return {'ok': True}

# --------------- Report ---------------

@app.get("/api/report/monthly")
def monthly_report(year: int, month: int):
    conn = get_db()
    start = f"{year}-{month:02d}-01T00:00:00"
    if month == 12:
        end = f"{year+1}-01-01T00:00:00"
    else:
        end = f"{year}-{month+1:02d}-01T00:00:00"

    rows = conn.execute("""
        SELECT direction, SUM(amount) as total, channel
        FROM transactions
        WHERE occurred_at >= ? AND occurred_at < ? AND direction != 'neutral' AND review_status != 'pending'
        GROUP BY direction, channel
    """, (start, end)).fetchall()

    total_expense = 0
    total_income = 0
    by_channel = {}
    for r in rows:
        ch = r['channel'] or 'unknown'
        if ch not in by_channel:
            by_channel[ch] = {'expense': 0, 'income': 0}
        if r['direction'] == 'expense':
            total_expense += (r['total'] or 0)
            by_channel[ch]['expense'] += (r['total'] or 0)
        elif r['direction'] == 'income':
            total_income += (r['total'] or 0)
            by_channel[ch]['income'] += (r['total'] or 0)

    living_net = total_income - total_expense

    # extraordinary items
    extra_expense = conn.execute(
        "SELECT SUM(amount) FROM transactions WHERE occurred_at >= ? AND occurred_at < ? AND flow_type = 'extraordinary_expense' AND review_status != 'pending'",
        (start, end)).fetchone()
    extra_income = conn.execute(
        "SELECT SUM(amount) FROM transactions WHERE occurred_at >= ? AND occurred_at < ? AND flow_type = 'extraordinary_income' AND review_status != 'pending'",
        (start, end)).fetchone()
    core_expense = total_expense - (extra_expense[0] or 0)
    core_income = total_income - (extra_income[0] or 0)
    core_net = core_income - core_expense

    # investment change from records
    inv_records = conn.execute("""
        SELECT SUM(CASE WHEN direction='profit' THEN amount ELSE -amount END) as total
        FROM investment_records WHERE date >= ? AND date < ?
    """, (start, end)).fetchone()
    investment_change = inv_records['total'] if inv_records and inv_records['total'] is not None else None

    net_profit = living_net + (investment_change or 0)

    conn.close()
    return {
        'year': year, 'month': month,
        'total_expense': total_expense,
        'total_income': total_income,
        'living_net': living_net,
        'core_expense': core_expense,
        'core_income': core_income,
        'core_net': core_net,
        'investment_change': investment_change,
        'net_profit': net_profit,
        'by_channel': by_channel,
    }

# --------------- Reconciliation ---------------

@app.get("/api/reconciliation/pending")
def get_pending():
    conn = get_db()
    count = conn.execute(
        "SELECT COUNT(*) as cnt FROM transactions WHERE review_status = 'pending'"
    ).fetchone()['cnt']
    conn.close()
    return {'pending': count}

@app.delete("/api/notification/clear")
def clear_notifications(month: str = None):
    """清除通知来源数据。?month=2026-05 只清当月，不传则清全部。"""
    conn = get_db()
    if month:
        start = f"{month}-01T00:00:00"
        y, m = month.split('-')
        y, m = int(y), int(m)
        if m == 12:
            end = f"{y+1}-01-01T00:00:00"
        else:
            end = f"{y}-{m+1:02d}-01T00:00:00"
        cur = conn.execute(
            "SELECT COUNT(*) as cnt FROM transactions WHERE review_status = 'pending' AND occurred_at >= ? AND occurred_at < ?",
            (start, end)
        )
    else:
        cur = conn.execute("SELECT COUNT(*) as cnt FROM transactions WHERE review_status = 'pending'")
    count = cur.fetchone()['cnt']

    if month:
        conn.execute(
            "DELETE FROM transactions WHERE review_status = 'pending' AND occurred_at >= ? AND occurred_at < ?",
            (start, end)
        )
    else:
        conn.execute("DELETE FROM transactions WHERE review_status = 'pending'")
    conn.commit()
    conn.close()
    return {'deleted': count}

# --------------- Investment Records ---------------

@app.post("/api/investment/record")
async def create_investment_record(data: dict):
    conn = get_db()
    rid = str(uuid.uuid4())
    now = now_cst()
    direction = data.get('direction', 'profit')
    amount = abs(float(data.get('amount', 0)))

    conn.execute(
        "INSERT INTO investment_records (id, date, direction, amount, recorded_at) VALUES (?, ?, ?, ?, ?)",
        (rid, data.get('date', now[:10]), direction, amount, now)
    )

    # update investment_value and total_asset buckets (both grow together)
    delta = amount if direction == 'profit' else -amount
    for bk in ['investment_value', 'total_asset']:
        cur = conn.execute("SELECT value FROM buckets WHERE key = ?", (bk,)).fetchone()
        if cur:
            new_val = (cur['value'] or 0) + delta
            conn.execute("UPDATE buckets SET value = ?, updated_at = ? WHERE key = ?", (new_val, now, bk))

    conn.commit()
    conn.close()
    return {'id': rid}

@app.get("/api/investment/records")
def list_investment_records():
    conn = get_db()
    rows = conn.execute("SELECT * FROM investment_records ORDER BY date DESC LIMIT 60").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.delete("/api/investment/record/{rid}")
def delete_investment_record(rid: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM investment_records WHERE id = ?", (rid,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Not found")
    # revert investment_value and total_asset
    delta = row['amount'] if row['direction'] == 'profit' else -row['amount']
    for bk in ['investment_value', 'total_asset']:
        cur = conn.execute("SELECT value FROM buckets WHERE key = ?", (bk,)).fetchone()
        if cur:
            new_val = (cur['value'] or 0) - delta
            conn.execute("UPDATE buckets SET value = ?, updated_at = ? WHERE key = ?", (new_val, now_cst(), bk))
    conn.execute("DELETE FROM investment_records WHERE id = ?", (rid,))
    conn.commit()
    conn.close()
    return {'ok': True}

# --------------- Root ---------------

@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, 'index.html'))

def start_server():
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8050, log_level="error")

if __name__ == '__main__':
    start_server()
    uvicorn.run(app, host="127.0.0.1", port=8050)
