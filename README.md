# MoneyMate

一款基于 Android + WebView + Chaquopy 的本地记账应用，支持微信/支付宝通知自动记账、账单导入、待确认流水管理、月报统计和资产科目管理。

## 主要功能

- 微信/支付宝通知自动记账
- 微信账单导入
- 支付宝 CSV 账单导入
- 待确认流水统一管理
- 流水手动编辑、删除、批量确认
- 月报与资产桶展示
- 本地 SQLite 数据存储

## 运行环境

- Android Studio 2024.x 或更新版本
- Android 8.0 及以上
- Java 17
- 支持 Chaquopy 的 Android 构建环境

## 本地运行

1. 用 Android Studio 打开仓库根目录。
2. 等待 Gradle 同步完成。
3. 连接真机或启动模拟器。
4. 点击 Run 运行 app。
5. 按系统提示开启通知使用权。

## 关键权限

- 通知使用权：用于自动识别微信/支付宝支付通知
- 网络权限：用于本地服务和页面通信
- 开机自启：用于设备重启后恢复通知监听
- 通知权限：Android 13 及以上需要

## 数据说明

- 所有记账数据保存在应用私有目录内的 SQLite 数据库中。
- 默认不会上传到云端。
- 卸载应用会清除本地数据。

## 开发说明

- Android 端入口在 `app/src/main/java/com/accounting`
- 本地 FastAPI 逻辑在 `app/src/main/python`
- 运行时页面在 `app/src/main/python/index.html`
- 构建镜像目录在 `app/build/python/sources/debug`

## 常见问题

### 为什么要重新 Run 才能看到改动？
因为页面和 Python 服务都被打包进 APK，必须重新构建安装后才会生效。

### 为什么某些手机上通知不稳定？
部分厂商会限制后台和通知监听服务，建议关闭电池优化并允许通知使用权。

## 截图

后续可补充。
