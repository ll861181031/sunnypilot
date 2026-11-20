package com.jixiexiaoge.drivingassist.ui.theme

import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/** 外观模式：跟随系统 / 强制深色 / 强制浅色 */
enum class ThemeMode(val storageValue: String) {
    SYSTEM("system"),
    DARK("dark"),
    LIGHT("light");

    companion object {
        fun fromStorage(value: String?): ThemeMode =
            entries.firstOrNull { it.storageValue == value } ?: SYSTEM
    }
}

/**
 * 外观状态（8.2.0 浅色模式）。
 *
 * 只做三件事：记住用户选择、跟随系统深浅色、把结果写进 [AppPaletteState]。
 * 写入是 Compose 状态，所以切换后所有读过颜色 token 的组合会立即重组，不需要重建 Activity。
 */
object AppearanceState {
    private const val PREFS_NAME = "CarrotAmap"
    private const val KEY_MODE = "appearance_mode"

    var mode by mutableStateOf(ThemeMode.SYSTEM)
        private set

    var systemDark by mutableStateOf(true)
        private set

    /** 当前是否深色（供 Compose 主题和需要分支的代码读） */
    fun resolveDark(systemIsDark: Boolean = systemDark): Boolean = when (mode) {
        ThemeMode.DARK -> true
        ThemeMode.LIGHT -> false
        ThemeMode.SYSTEM -> systemIsDark
    }

    fun isDark(): Boolean = resolveDark()

    /** 启动时调用：读偏好 + 传入系统深浅色，第一帧就是对的配色 */
    fun load(context: Context, systemIsDark: Boolean) {
        systemDark = systemIsDark
        val prefs = context.applicationContext.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        mode = ThemeMode.fromStorage(prefs.getString(KEY_MODE, null))
        applyPalette()
    }

    /** 系统深浅色变化（CompositionLocalConfiguration 驱动，只在真正变化时生效） */
    fun syncSystemDark(systemIsDark: Boolean) {
        if (systemDark == systemIsDark) return
        systemDark = systemIsDark
        applyPalette()
    }

    /** 用户在「关于 · 外观」里选择 */
    fun setMode(context: Context, newMode: ThemeMode) {
        mode = newMode
        context.applicationContext
            .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_MODE, newMode.storageValue)
            .apply()
        applyPalette()
    }

    private fun applyPalette() {
        AppPaletteState = if (resolveDark()) DarkPalette else LightPalette
    }
}
