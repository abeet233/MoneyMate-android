package com.accounting

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.system.Os
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

class MainActivity : AppCompatActivity() {
    private var filePathCallback: ValueCallback<Array<Uri>>? = null
    private lateinit var webView: WebView
    private var lastBackPressedAt: Long = 0

    companion object {
        private const val FILE_CHOOSER_REQUEST_CODE = 1001
        private const val NOTIFICATION_PERMISSION_REQUEST_CODE = 1002
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Set data dir for Python (database location)
        val dataDir = filesDir.absolutePath
        Os.setenv("ACCOUNTING_DATA_DIR", dataDir, true)

        // Start Python Uvicorn in background
        Thread {
            if (!Python.isStarted()) Python.start(AndroidPlatform(this@MainActivity))
            Python.getInstance().getModule("main").callAttr("start_server")
        }.start()

        // WebView - retry loading until server is ready
        webView = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.loadWithOverviewMode = true
            settings.useWideViewPort = true
            settings.allowFileAccess = true
            settings.allowContentAccess = true
            settings.builtInZoomControls = false
            settings.displayZoomControls = false
            settings.setSupportZoom(false)
            settings.textZoom = 100
            settings.mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            isVerticalScrollBarEnabled = false
            isHorizontalScrollBarEnabled = false
            webViewClient = WebViewClient()
            webChromeClient = object : WebChromeClient() {
                override fun onShowFileChooser(
                    webView: WebView?,
                    filePathCallback: ValueCallback<Array<Uri>>?,
                    fileChooserParams: FileChooserParams?
                ): Boolean {
                    this@MainActivity.filePathCallback?.onReceiveValue(null)
                    this@MainActivity.filePathCallback = filePathCallback

                    // Accept all file types to support CSV, xlsx, etc.
                    val acceptTypes = fileChooserParams?.acceptTypes?.filter { it != null } ?: emptyList()
                    val mimeTypes = if (acceptTypes.isEmpty() || acceptTypes.any { it == "*/*" || it == "" })
                        arrayOf("*/*")
                    else
                        acceptTypes.toTypedArray()

                    val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                        addCategory(Intent.CATEGORY_OPENABLE)
                        type = "*/*"
                        putExtra(Intent.EXTRA_MIME_TYPES, mimeTypes)
                        putExtra(Intent.EXTRA_ALLOW_MULTIPLE, false)
                    }

                    return try {
                        startActivityForResult(intent, FILE_CHOOSER_REQUEST_CODE)
                        true
                    } catch (_: ActivityNotFoundException) {
                        this@MainActivity.filePathCallback = null
                        Toast.makeText(this@MainActivity, "没有可用的文件选择器", Toast.LENGTH_SHORT).show()
                        false
                    }
                }
            }
        }

        setContentView(webView)
        requestNotificationPermissionIfNeeded()
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                webView.evaluateJavascript("window.handleAndroidBack && window.handleAndroidBack();") { handled ->
                    if (handled == "true") return@evaluateJavascript

                    val now = System.currentTimeMillis()
                    if (now - lastBackPressedAt < 2000) {
                        finish()
                    } else {
                        lastBackPressedAt = now
                        Toast.makeText(this@MainActivity, "再按一次退出", Toast.LENGTH_SHORT).show()
                    }
                }
            }
        })

        // Poll until server is ready, then load
        val handler = Handler(Looper.getMainLooper())
        var attempts = 0
        val poll = object : Runnable {
            override fun run() {
                attempts++
                if (attempts > 20) {
                    webView.loadData("<h3>服务启动超时，请重启App</h3>", "text/html", "UTF-8")
                    return
                }
                Thread {
                    try {
                        val url = java.net.URL("http://127.0.0.1:8050/api/settings")
                        val conn = url.openConnection() as java.net.HttpURLConnection
                        conn.connectTimeout = 500
                        conn.readTimeout = 500
                        if (conn.responseCode == 200) {
                            handler.post { webView.loadUrl("http://127.0.0.1:8050/?mobile=1") }
                            return@Thread
                        }
                    } catch (_: Exception) {}
                    handler.postDelayed(this, 500)
                }.start()
            }
        }
        handler.postDelayed(poll, 1000)

        // Notification hint
        Handler(Looper.getMainLooper()).postDelayed({
            try { startService(android.content.Intent(this, NotificationListener::class.java)) } catch (_: Exception) {}
            if (!isListenerEnabled()) {
                Toast.makeText(this, "通知自动记账未开启\n设置 → 通知与状态栏 → 通知使用权 → 实时记账", Toast.LENGTH_LONG).show()
            }
        }, 4000)
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return
        ActivityCompat.requestPermissions(
            this,
            arrayOf(Manifest.permission.POST_NOTIFICATIONS),
            NOTIFICATION_PERMISSION_REQUEST_CODE
        )
    }

    private fun isListenerEnabled(): Boolean {
        val flat = Settings.Secure.getString(contentResolver, "enabled_notification_listeners") ?: ""
        return flat.contains(ComponentName(this, NotificationListener::class.java).flattenToString())
    }

    @Deprecated("Deprecated in Android API, but still the simplest WebView file chooser bridge here.")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        if (requestCode == FILE_CHOOSER_REQUEST_CODE) {
            val callback = filePathCallback
            filePathCallback = null
            val result = if (resultCode == Activity.RESULT_OK) {
                WebChromeClient.FileChooserParams.parseResult(resultCode, data)
            } else {
                null
            }
            callback?.onReceiveValue(result)
            return
        }
        super.onActivityResult(requestCode, resultCode, data)
    }
}
