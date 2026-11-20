package com.jixiexiaoge.drivingassist.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * 车载主题（8.2.0 起支持深色 / 浅色 / 跟随系统）。
 *
 * 配色来自 [AppPaletteState]，切换外观时 token 会整体换掉，调用点无需改。
 * 具体用哪套由 [AppearanceState] 决定：用户在「关于 · 外观」里选择，默认跟随系统。
 */
private fun appColorScheme(palette: AppPalette) = darkColorScheme(
    primary = palette.accentCyan,
    onPrimary = palette.onBrand,
    primaryContainer = palette.accentCyan.copy(alpha = 0.15f),
    onPrimaryContainer = palette.accentCyan,

    secondary = palette.secondary,
    onSecondary = palette.onBrand,
    secondaryContainer = palette.secondary.copy(alpha = 0.15f),
    onSecondaryContainer = palette.secondary,

    tertiary = palette.accentPurple,
    onTertiary = palette.onBrand,

    background = palette.surface900,
    onBackground = palette.textPrimary,

    surface = palette.surface800,
    onSurface = palette.textPrimary,
    surfaceVariant = palette.surface700,
    onSurfaceVariant = palette.textSecondary,

    outline = palette.surface600,
    outlineVariant = palette.surface500,

    error = palette.error,
    onError = palette.onBrand,
    errorContainer = if (palette === DarkPalette) Color(0xFF7F1D1D) else Color(0xFFFEE2E2),
    onErrorContainer = if (palette === DarkPalette) Color(0xFFFECACA) else Color(0xFF991B1B),
)

/** 浅色主题的 Material 配色（背景更亮、文字更深，其余同上） */
private fun appLightColorScheme(palette: AppPalette): androidx.compose.material3.ColorScheme =
    lightColorScheme(
        primary = palette.accentCyan,
        onPrimary = palette.onBrand,
        primaryContainer = palette.accentCyan.copy(alpha = 0.12f),
        onPrimaryContainer = palette.accentCyan,

        secondary = palette.secondary,
        onSecondary = palette.onBrand,
        secondaryContainer = palette.secondary.copy(alpha = 0.12f),
        onSecondaryContainer = palette.secondary,

        tertiary = palette.accentPurple,
        onTertiary = palette.onBrand,

        background = palette.surface900,
        onBackground = palette.textPrimary,

        surface = palette.surface800,
        onSurface = palette.textPrimary,
        surfaceVariant = palette.surface700,
        onSurfaceVariant = palette.textSecondary,

        outline = palette.surface600,
        outlineVariant = palette.surface500,

        error = palette.error,
        onError = palette.onBrand,
        errorContainer = Color(0xFFFEE2E2),
        onErrorContainer = Color(0xFF991B1B),
    )

// ============================================
// Design Tokens — Spacing & Radius
// ============================================
data class AppSpacing(
    val xs: Dp = 4.dp,
    val sm: Dp = 8.dp,
    val md: Dp = 12.dp,
    val lg: Dp = 16.dp,
    val xl: Dp = 24.dp,
    val xxl: Dp = 32.dp,
)

data class AppRadius(
    val sm: Dp = 8.dp,
    val md: Dp = 12.dp,
    val lg: Dp = 16.dp,
)

val LocalAppSpacing = staticCompositionLocalOf { AppSpacing() }
val LocalAppRadius = staticCompositionLocalOf { AppRadius() }

@Composable
fun NavipilotTheme(
    content: @Composable () -> Unit
) {
    val systemIsDark = isSystemInDarkTheme()
    // 系统深浅色变化时同步到调色板（用户选了强制深/浅色时这里不会改配色）
    SideEffect { AppearanceState.syncSystemDark(systemIsDark) }

    val dark = AppearanceState.resolveDark(systemIsDark)
    val palette = if (dark) DarkPalette else LightPalette
    val scheme = if (dark) appColorScheme(palette) else appLightColorScheme(palette)

    MaterialTheme(
        colorScheme = scheme,
        typography  = Typography,
        content     = content
    )
}
