package com.jixiexiaoge.drivingassist.ui.theme

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.graphics.Color

// ============================================
// Design System — 双调色板（深色 / 浅色）
// ============================================
//
// 8.2.0：原来这里的 val 都是编译期常量，只够固定深色主题。
// 现在改成「调色板 + 只读取值的 token」：
//   - `AppPaletteState` 是一个 Compose 状态，切换时所有读过 token 的组合会自动重组；
//   - 画布/绘制阶段也会因为 snapshot 状态读取而重绘；
//   - 调用点完全不用改（仍旧写 `Surface800`、`TextSecondary`）。
// 只有 DriveViewCard 的仪表画布、7706 调试浮层、会员/投屏品牌卡是「固定配色画作」，
// 它们直接写死色值，不跟随主题。

data class AppPalette(
    // Primary
    val primary: Color,
    val primaryVariant: Color,
    val primaryLight: Color,
    val primaryDark: Color,
    // Secondary
    val secondary: Color,
    val secondaryVariant: Color,
    val secondaryLight: Color,
    val secondaryDark: Color,
    // Accent
    val accent: Color,
    val accentOrange: Color,
    val accentPurple: Color,
    val accentCyan: Color,
    // Surfaces（900 = 页面底，800 = 卡片，700/600 = 描边与浅底）
    val surface900: Color,
    val surface800: Color,
    val surface700: Color,
    val surface600: Color,
    val surface500: Color,
    val surface400: Color,
    val surface300: Color,
    val surface200: Color,
    val surface100: Color,
    val surface50: Color,
    // Semantic
    val success: Color,
    val warning: Color,
    val error: Color,
    val info: Color,
    // Text
    val textPrimary: Color,
    val textSecondary: Color,
    val textTertiary: Color,
    val textDisabled: Color,
    // Buttons
    val buttonHome: Color,
    val buttonCompany: Color,
    val buttonAdvanced: Color,
    val buttonHelp: Color,
    val buttonAccel: Color,
    val buttonDecel: Color,
    val buttonLaneChange: Color,
    val buttonReport: Color,
    val buttonExperiment: Color,
    // Overtake
    val overtakeDisabled: Color,
    val overtakeManual: Color,
    val overtakeAuto: Color,
    // Containers
    val cardBackground: Color,
    val cardBackgroundDark: Color,
    val dialogBackground: Color,
    val overlayBackground: Color,
    /** 品牌色块（会员/投屏/按钮）上的文字，两种主题都是纯白 */
    val onBrand: Color,
)

/** 深色主题：车载夜间默认 */
internal val DarkPalette = AppPalette(
    primary = Color(0xFF2563EB),
    primaryVariant = Color(0xFF1D4ED8),
    primaryLight = Color(0xFF60A5FA),
    primaryDark = Color(0xFF1E40AF),
    secondary = Color(0xFF10B981),
    secondaryVariant = Color(0xFF059669),
    secondaryLight = Color(0xFF22C55E),
    secondaryDark = Color(0xFF047857),
    accent = Color(0xFF3B82F6),
    accentOrange = Color(0xFFF97316),
    accentPurple = Color(0xFF8B5CF6),
    accentCyan = Color(0xFF60A5FA),
    surface900 = Color(0xFF070E1F),
    surface800 = Color(0xFF0F1A30),
    surface700 = Color(0xFF1B2A46),
    surface600 = Color(0xFF374151),
    surface500 = Color(0xFF64748B),
    surface400 = Color(0xFF94A3B8),
    surface300 = Color(0xFFCBD5E1),
    surface200 = Color(0xFFE2E8F0),
    surface100 = Color(0xFFF1F5F9),
    surface50 = Color(0xFFF8FAFC),
    success = Color(0xFF10B981),
    warning = Color(0xFFF97316),
    error = Color(0xFFEF4444),
    info = Color(0xFF3B82F6),
    textPrimary = Color(0xFFF8FAFC),
    textSecondary = Color(0xFFCBD5E1),
    textTertiary = Color(0xFF94A3B8),
    textDisabled = Color(0xFF64748B),
    buttonHome = Color(0xFFFFD700),
    buttonCompany = Color(0xFFFF8C00),
    buttonAdvanced = Color(0xFFF97316),
    buttonHelp = Color(0xFF3B82F6),
    buttonAccel = Color(0xFF10B981),
    buttonDecel = Color(0xFFEF4444),
    buttonLaneChange = Color(0xFF3B82F6),
    buttonReport = Color(0xFF8B5CF6),
    buttonExperiment = Color(0xFFF97316),
    overtakeDisabled = Color(0xFF94A3B8),
    overtakeManual = Color(0xFF3B82F6),
    overtakeAuto = Color(0xFF10B981),
    cardBackground = Color(0xFFFFFFFF),
    cardBackgroundDark = Color(0xFF111827),
    dialogBackground = Color(0xFF111827),
    overlayBackground = Color(0xFF070E1F).copy(alpha = 0.92f),
    onBrand = Color(0xFFFFFFFF),
)

/** 浅色主题：白天 / 车内强光。页面用冷灰白，卡片纯白，文字深墨。 */
internal val LightPalette = AppPalette(
    primary = Color(0xFF1D4ED8),
    primaryVariant = Color(0xFF1E40AF),
    primaryLight = Color(0xFF2563EB),
    primaryDark = Color(0xFF1E3A8A),
    secondary = Color(0xFF047857),
    secondaryVariant = Color(0xFF065F46),
    secondaryLight = Color(0xFF15803D),
    secondaryDark = Color(0xFF064E3B),
    // 蓝白调：强调色全部走蓝，橙/紫只留给"注意/实验"语义
    accent = Color(0xFF1D4ED8),
    accentOrange = Color(0xFFC2410C),
    accentPurple = Color(0xFF5B21B6),
    accentCyan = Color(0xFF2563EB),
    surface900 = Color(0xFFE9F1FD),
    surface800 = Color(0xFFFFFFFF),
    surface700 = Color(0xFFDCE8FB),
    surface600 = Color(0xFFC3D6F2),
    surface500 = Color(0xFF9CB2D4),
    surface400 = Color(0xFF7189AC),
    surface300 = Color(0xFF4C6485),
    surface200 = Color(0xFF33496B),
    surface100 = Color(0xFF22344F),
    surface50 = Color(0xFF131F33),
    success = Color(0xFF047857),
    warning = Color(0xFFB45309),
    error = Color(0xFFDC2626),
    info = Color(0xFF1D4ED8),
    textPrimary = Color(0xFF0B2545),
    textSecondary = Color(0xFF2C4A72),
    textTertiary = Color(0xFF5A7595),
    textDisabled = Color(0xFF8FA6C2),
    buttonHome = Color(0xFFB45309),
    buttonCompany = Color(0xFFC2410C),
    buttonAdvanced = Color(0xFF1D4ED8),
    buttonHelp = Color(0xFF1D4ED8),
    buttonAccel = Color(0xFF047857),
    buttonDecel = Color(0xFFDC2626),
    buttonLaneChange = Color(0xFF1D4ED8),
    buttonReport = Color(0xFF5B21B6),
    buttonExperiment = Color(0xFFC2410C),
    overtakeDisabled = Color(0xFF8FA6C2),
    overtakeManual = Color(0xFF1D4ED8),
    overtakeAuto = Color(0xFF047857),
    cardBackground = Color(0xFFFFFFFF),
    cardBackgroundDark = Color(0xFFFFFFFF),
    dialogBackground = Color(0xFFFFFFFF),
    overlayBackground = Color(0xFFE9F1FD).copy(alpha = 0.96f),
    onBrand = Color(0xFFFFFFFF),
)

/**
 * 当前调色板。写成 Compose 状态：切换主题时，所有读过 token 的组合/绘制都会失效重建。
 * 只允许 [AppearanceState] 写它。
 */
internal var AppPaletteState by mutableStateOf(DarkPalette)

internal val isLightPalette: Boolean get() = AppPaletteState === LightPalette

/** 当前是否浅色外观（给需要换语义色的组件用） */
val isLightTheme: Boolean get() = AppPaletteState === LightPalette

// ============================================
// Tokens（调用点不需要改）
// ============================================

val Primary: Color get() = AppPaletteState.primary
val PrimaryVariant: Color get() = AppPaletteState.primaryVariant
val PrimaryLight: Color get() = AppPaletteState.primaryLight
val PrimaryDark: Color get() = AppPaletteState.primaryDark

val Secondary: Color get() = AppPaletteState.secondary
val SecondaryVariant: Color get() = AppPaletteState.secondaryVariant
val SecondaryLight: Color get() = AppPaletteState.secondaryLight
val SecondaryDark: Color get() = AppPaletteState.secondaryDark

val Accent: Color get() = AppPaletteState.accent
val AccentOrange: Color get() = AppPaletteState.accentOrange
val AccentPurple: Color get() = AppPaletteState.accentPurple
val AccentCyan: Color get() = AppPaletteState.accentCyan

val Surface900: Color get() = AppPaletteState.surface900
val Surface800: Color get() = AppPaletteState.surface800
val Surface700: Color get() = AppPaletteState.surface700
val Surface600: Color get() = AppPaletteState.surface600
val Surface500: Color get() = AppPaletteState.surface500
val Surface400: Color get() = AppPaletteState.surface400
val Surface300: Color get() = AppPaletteState.surface300
val Surface200: Color get() = AppPaletteState.surface200
val Surface100: Color get() = AppPaletteState.surface100
val Surface50: Color get() = AppPaletteState.surface50

val Success: Color get() = AppPaletteState.success
val Warning: Color get() = AppPaletteState.warning
val Error: Color get() = AppPaletteState.error
val Info: Color get() = AppPaletteState.info

val TextPrimary: Color get() = AppPaletteState.textPrimary
val TextSecondary: Color get() = AppPaletteState.textSecondary
val TextTertiary: Color get() = AppPaletteState.textTertiary
val TextDisabled: Color get() = AppPaletteState.textDisabled

val SpeedCruise: Color get() = AppPaletteState.info
val SpeedCurrent: Color get() = AppPaletteState.success

val ButtonHome: Color get() = AppPaletteState.buttonHome
val ButtonCompany: Color get() = AppPaletteState.buttonCompany
val ButtonAdvanced: Color get() = AppPaletteState.buttonAdvanced
val ButtonHelp: Color get() = AppPaletteState.buttonHelp
val ButtonAccel: Color get() = AppPaletteState.buttonAccel
val ButtonDecel: Color get() = AppPaletteState.buttonDecel
val ButtonLaneChange: Color get() = AppPaletteState.buttonLaneChange
val ButtonReport: Color get() = AppPaletteState.buttonReport
val ButtonExperiment: Color get() = AppPaletteState.buttonExperiment

val OvertakeDisabled: Color get() = AppPaletteState.overtakeDisabled
val OvertakeManual: Color get() = AppPaletteState.overtakeManual
val OvertakeAuto: Color get() = AppPaletteState.overtakeAuto

val CardBackground: Color get() = AppPaletteState.cardBackground
val CardBackgroundDark: Color get() = AppPaletteState.cardBackgroundDark
val DialogBackground: Color get() = AppPaletteState.dialogBackground
val OverlayBackground: Color get() = AppPaletteState.overlayBackground

/** 品牌色块上的文字/图标：两种主题都是纯白 */
val OnBrand: Color get() = AppPaletteState.onBrand
