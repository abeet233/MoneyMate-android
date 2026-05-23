package com.accounting

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.os.Build
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import androidx.core.app.NotificationCompat
import com.google.gson.Gson
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

class NotificationListener : NotificationListenerService() {
    private val JSON = "application/json; charset=utf-8".toMediaType()
    private val gson = Gson()
    private val client = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS)
        .writeTimeout(5, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .build()

    private val recentCache = object : LinkedHashMap<String, Long>(30, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<String, Long>?): Boolean =
            size > 30
    }

    companion object {
        const val CHANNEL_ID = "accounting_foreground"
        const val NOTIFICATION_ID = 1
        const val PREFS_NAME = "accounting_prefs"
        const val KEY_ENABLED = "notification_enabled"
        const val KEY_BACKEND_URL = "backend_url"
        const val DEFAULT_URL = "http://127.0.0.1:8050/api/events"

        const val WECHAT_PKG = "com.tencent.mm"
        const val ALIPAY_PKG = "com.eg.android.AlipayGphone"

        val AMOUNT_REGEX = Regex("""(?:[¥￥]\s*(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*元)""")
        val EXPENSE_KEYWORDS = listOf("支付", "付款", "扣款", "消费")
        val INCOME_KEYWORDS = listOf("收款", "退款", "到账", "转入")
        val WECHAT_PAYMENT_KEYWORDS = listOf("微信支付", "收款码", "收款", "收到", "到账", "已到账", "入账", "转账", "退款", "红包", "支付", "付款", "付款成功", "支付成功", "扣款", "消费")
        val ALIPAY_PAYMENT_KEYWORDS = listOf("交易提醒", "退款提醒", "支付宝", "花呗", "余额宝", "支付", "付款", "付款成功", "支付成功", "扣款", "消费", "收款", "收到", "到账", "已到账", "入账", "退款", "转账")
    }

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForeground(NOTIFICATION_ID, buildForegroundNotification())
        return START_STICKY
    }

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        if (sbn == null) return
        if (!isEnabled()) return

        val pkg = sbn.packageName
        if (pkg != WECHAT_PKG && pkg != ALIPAY_PKG) return

        val notif = sbn.notification
        val extras = notif.extras
        val title = extras.getString(Notification.EXTRA_TITLE, "")
        val text = extras.getString(Notification.EXTRA_TEXT, "")
        val bigText = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString() ?: ""
        val subText = extras.getString(Notification.EXTRA_SUB_TEXT, "")

        val fullText = listOf(title, text, bigText, subText)
            .filter { it.isNotBlank() }
            .joinToString(" ")

        if (fullText.isBlank()) return

        if (!shouldRecordNotification(pkg, title, fullText)) return

        val amount = parseAmount(fullText)
        val direction = parseDirection(fullText)
        val app = if (pkg == WECHAT_PKG) "wechat" else "alipay"

        val cacheKey = "${app}_${amount}_${md5Short(fullText)}"
        val now = System.currentTimeMillis()
        synchronized(recentCache) {
            val last = recentCache[cacheKey]
            if (last != null && now - last < 5000) return
            recentCache[cacheKey] = now
        }

        val payload = mapOf(
            "app" to app,
            "title" to title,
            "text" to fullText,
            "amount" to (amount?.toString() ?: ""),
            "direction" to direction,
            "timestamp" to java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS", java.util.Locale.getDefault())
                .format(java.util.Date(sbn.postTime))
        )

        sendToBackend(payload)
    }

    override fun onNotificationRemoved(sbn: StatusBarNotification?) {}

    private fun parseAmount(text: String): Double? {
        val matches = AMOUNT_REGEX.findAll(text)
        for (match in matches) {
            val value = match.groups[1]?.value ?: match.groups[2]?.value
            if (!value.isNullOrBlank()) {
                return value.toDoubleOrNull()
            }
        }
        return null
    }

    private fun shouldRecordNotification(pkg: String, title: String, text: String): Boolean {
        val combined = "$title $text"
        return when (pkg) {
            WECHAT_PKG -> WECHAT_PAYMENT_KEYWORDS.any { combined.contains(it) }
            ALIPAY_PKG -> ALIPAY_PAYMENT_KEYWORDS.any { combined.contains(it) }
            else -> false
        }
    }

    private fun parseDirection(text: String): String {
        for (kw in INCOME_KEYWORDS) {
            if (text.contains(kw)) return "income"
        }
        for (kw in EXPENSE_KEYWORDS) {
            if (text.contains(kw)) return "expense"
        }
        return "expense"
    }

    private fun sendToBackend(data: Map<String, String>) {
        val url = getBackendUrl()
        val body = gson.toJson(data).toRequestBody(JSON)
        val request = Request.Builder().url(url).post(body).build()
        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {}
            override fun onResponse(call: Call, response: Response) { response.close() }
        })
    }

    private fun md5Short(input: String): String {
        val md = MessageDigest.getInstance("MD5")
        return md.digest(input.toByteArray()).take(4).joinToString("") { "%02x".format(it) }
    }

    private fun isEnabled(): Boolean {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getBoolean(KEY_ENABLED, true)
    }

    private fun getBackendUrl(): String {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getString(KEY_BACKEND_URL, DEFAULT_URL) ?: DEFAULT_URL
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID, "记账服务", NotificationManager.IMPORTANCE_LOW
            ).apply { description = "后台通知监听服务运行中" }
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }
    }

    private fun buildForegroundNotification(): Notification {
        val intent = Intent(this, MainActivity::class.java)
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) PendingIntent.FLAG_IMMUTABLE else 0
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("记账服务运行中")
            .setContentText("正在监听微信/支付宝支付通知")
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .build()
    }
}
