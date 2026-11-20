package com.jixiexiaoge.drivingassist

import androidx.compose.runtime.collectAsState

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.VpnService
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.ui.draw.clip
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import com.jixiexiaoge.drivingassist.ui.theme.Surface700
import com.jixiexiaoge.drivingassist.ui.theme.Surface600
import com.jixiexiaoge.drivingassist.ui.theme.Surface800
import com.jixiexiaoge.drivingassist.ui.theme.Surface900
import com.jixiexiaoge.drivingassist.ui.theme.TextSecondary
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.jixiexiaoge.drivingassist.ui.components.AutoSwitchExperimentPage
import com.jixiexiaoge.drivingassist.ui.components.AdaptiveCardGrid
import com.jixiexiaoge.drivingassist.ui.components.AdaptivePageColumn
import com.jixiexiaoge.drivingassist.ui.components.HomeDashboardTabs
import com.jixiexiaoge.drivingassist.ui.components.rememberAmapNavigationDisplay
import com.jixiexiaoge.drivingassist.data.toDisplaySnapshot
import com.jixiexiaoge.drivingassist.data.CommaConnectionDisplay
import com.jixiexiaoge.drivingassist.data.resolveCommaConnectionDisplay
import com.jixiexiaoge.drivingassist.ui.components.Carrot7706JsonDebugOverlay
import com.jixiexiaoge.drivingassist.ui.theme.NavipilotTheme
import com.jixiexiaoge.drivingassist.ui.utils.localized
import kotlinx.coroutines.launch
import kotlinx.coroutines.delay
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.isActive
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Slider
import androidx.compose.material3.SliderDefaults
import androidx.compose.material3.Switch
import androidx.compose.foundation.layout.heightIn
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import com.navipilot.amap2tesla.ConnectionMode
import com.jixiexiaoge.drivingassist.core.Result
import com.jixiexiaoge.drivingassist.ui.theme.PrimaryLight
import com.jixiexiaoge.drivingassist.ui.theme.TextPrimary
import com.jixiexiaoge.drivingassist.ui.theme.Warning
import com.jixiexiaoge.drivingassist.ui.theme.OnBrand
import com.jixiexiaoge.drivingassist.ui.theme.Primary

/** 「回家 / 公司」导航请求结果：是否已入队 + 给用户看的提示文案。 */
internal data class DestinationNavigationResult(
    val accepted: Boolean,
    val message: String,
)

/**
 * MainActivity UI组件管理类
 * 负责所有UI组件的定义和界面逻辑
 */
@OptIn(ExperimentalLayoutApi::class)
class MainActivityUI(
    private val core: MainActivityCore
) {

    /**
     * 设置用户界面
     */
    @Composable
    fun SetupUserInterface() {
        NavipilotTheme {
            Box(modifier = Modifier.fillMaxSize().background(Surface900).safeDrawingPadding()) {
                // 拦截返回键：正常返回
                BackHandler(enabled = true) {
                    if (core.currentPage !is Page.CpFunctions) {
                        core.currentPage = Page.CpFunctions
                    }
                    // CP 功能页时什么都不做，防止退出应用
                }

                Column(modifier = Modifier.fillMaxSize()) {
                    when (core.currentPage) {
                        else -> Unit
                    }
                    Box(modifier = Modifier.weight(1f)) {
                    // 根据当前页面显示不同内容
                    when (core.currentPage) {
                        is Page.CpFunctions -> HomePage(
                            userType = core.userType.value,
                            carrotManFields = core.carrotManFields.value,
                            onSendCommand = { command, arg -> core.sendCarrotCommand(command, arg) },
                            onSendRoadLimitSpeed = { core.sendCurrentRoadLimitSpeed() },
                            onLaunchAmap = { core.launchAmapAuto() },
                            onSendNavConfirmation = { core.sendNavigationConfirmationManually() },
                        )
                        is Page.Experiment -> AutoSwitchExperimentPage(
                            onBack = {
                                core.currentPage = Page.CpFunctions
                            },
                            conditionalExperimentManager = core.getConditionalExperimentManagerSafely(),
                            carrotParamClient = core.getCarrotParamClientSafely(),
                            carrotManFields = core.carrotManFields,
                            automationObservation = core.automationObservation,
                        )
                        is Page.SmartSpeed -> SmartSpeedPage(
                            onBack = {
                                core.currentPage = Page.CpFunctions
                            },
                            fields = core.carrotManFields.value,
                        )
                    }
                }
                }

            }

        }
    }

    /**
     * 主页组件
     */
    @Composable
    private fun HomePage(
        userType: Int,
        carrotManFields: CarrotManFields,
        onSendCommand: (String, String) -> Unit,
        onSendRoadLimitSpeed: () -> Unit,
        onLaunchAmap: () -> Unit,
        onSendNavConfirmation: () -> Unit,
    ) {
        // 点击首页预览条：全屏 7706 JSON 调试
        var show7706JsonDebug by remember { mutableStateOf(false) }
        val carrotFieldsLive by core.carrotManFields

        fun connectionDisplayNow(): CommaConnectionDisplay {
            val client = core.getNetworkClientSafely()
            val compact = core.carrotTelemetry.value
            return resolveCommaConnectionDisplay(
                client?.isRunning() == true, client?.getCurrentDevice() != null,
                core.carrotWsClient?.connectionState?.value
                    ?: com.jixiexiaoge.drivingassist.data.ConnectionState.DISCONNECTED,
                core.wsDataTimeout.value,
                compact?.let { value -> value.services.keys.any { value.fresh(it) != null } } == true,
            )
        }
        val owner = LocalLifecycleOwner.current
        var commaConnectionDisplay by remember(core, owner) { mutableStateOf(connectionDisplayNow()) }
        LaunchedEffect(core, owner) {
            owner.lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
                while (isActive) {
                    commaConnectionDisplay = connectionDisplayNow()
                    delay(500)
                }
            }
        }

        // ===== 垂直布局：数据面板 + 底部控制栏 =====
        Column(modifier = Modifier.fillMaxSize()) {
            // 顶部：数据面板（占据主要空间），无地图时纯深色背景
            Box(
                modifier = Modifier
                    .fillMaxWidth()
                    .weight(1f)
                    .background(Surface900)
            ) {
                // 数据面板（居中显示）
                // 注意：旧实现还传了 cruiseSetSpeed / carCruiseSpeed / vehicleData 三个参数，
                // 而 HomeControlPanel 体内一次都没用到，却让调用方每次重组都构造一遍完整
                // VehicleData 对象图（20Hz 车端数据 → 每秒白算 20 次）。已删除。
                HomeControlPanel(
                    modifier = Modifier
                        .fillMaxSize(),
                    carrotManFields = carrotManFields,
                    userType = userType,
                    carrotParamClient = core.getCarrotParamClientSafely(),
                    commaConnectionDisplay = commaConnectionDisplay,
                    onPageChange = { page: Page -> core.currentPage = page },
                    onSendCommand = onSendCommand,
                    onSendRoadLimitSpeed = onSendRoadLimitSpeed,
                    onLaunchAmap = onLaunchAmap,
                    onSendNavConfirmation = onSendNavConfirmation,
                    onShow7706Debug = { show7706JsonDebug = true },
                )
            }

        }

        // 7706 JSON 调试面板（点击车道盲区面板触发）
        if (show7706JsonDebug) {
            Carrot7706JsonDebugOverlay(
                fields = carrotFieldsLive,
                networkClient = core.getNetworkClientSafely(),
                v2Client = core.naviV2Client,
                streamManager = core.naviStreamManager,
                broadcastManager = core.getAmapBroadcastManagerOrNull(),
                bridge = core.xiaogeBridge,
                onDismiss = { show7706JsonDebug = false }
            )
        }

    }

    /**
     * 首页功能控制面板
     */
    @Composable
    private fun HomeControlPanel(
        modifier: Modifier = Modifier,
        carrotManFields: CarrotManFields,
        userType: Int,
        carrotParamClient: CarrotParamClient?,
        commaConnectionDisplay: CommaConnectionDisplay = CommaConnectionDisplay.Disconnected,
        onPageChange: (Page) -> Unit,
        onSendCommand: (String, String) -> Unit = { _, _ -> },
        onSendRoadLimitSpeed: () -> Unit = {},
        onLaunchAmap: () -> Unit = {},
        onSendNavConfirmation: () -> Unit = {},
        onShow7706Debug: () -> Unit,
    ) {
        val panelContext = LocalContext.current
        var aboutOpenTrigger by rememberSaveable { mutableIntStateOf(0) }
        // 只在原始 telemetry 变化时重建显示快照（20Hz 数据 + 每次重组都全量映射 = 掉帧主因之一）
        val telemetryRaw = core.carrotTelemetry.value
        val telemetryDisplay = remember(telemetryRaw) { telemetryRaw.toDisplaySnapshot() }
        val effectiveDrivingMode = if (core.automaticDrivingMode) core.detectedDrivingMode else core.drivingMode
        val amapDisplay = rememberAmapNavigationDisplay(
            carrotManFields, core.naviSources, core.activeNavMode.value, core::sampleAutomaticDrivingMode,
        )

        // 解析设备端 ExperimentalMode 参数（与旧 SecondarySection 一致）
        fun parseExperimentalMode(value: Any?): Boolean? {
            return when (value) {
                is Boolean -> value
                is Int -> value != 0
                is Long -> value != 0L
                is Double -> value != 0.0
                is Float -> value != 0f
                is String -> value == "1" || value.equals("true", ignoreCase = true)
                else -> null
            }
        }

        val coroutineScope = rememberCoroutineScope()
        // 从设备读取 AlwaysLateral 参数值
        var isAlwaysLateral by remember(carrotParamClient) { mutableStateOf<Boolean?>(null) }

        LaunchedEffect(carrotParamClient) {
            if (carrotParamClient == null) {
                isAlwaysLateral = null
                return@LaunchedEffect
            }
            val result = carrotParamClient.getParams("AlwaysLateral")
            isAlwaysLateral = result.getOrNull()?.get("AlwaysLateral")?.let(::parseExperimentalMode)
        }

        // 从设备读取 ShareData 参数值
        var isShareDataEnabled by remember(carrotParamClient) { mutableStateOf<Boolean?>(null) }

        LaunchedEffect(carrotParamClient) {
            if (carrotParamClient == null) {
                isShareDataEnabled = null
                return@LaunchedEffect
            }
            val result = carrotParamClient.getParams("ShareData")
            isShareDataEnabled = result.getOrNull()?.get("ShareData")?.let(::parseExperimentalMode)
        }

        val onAlwaysLateralClick: () -> Unit = {
            if (carrotParamClient == null) {
                android.widget.Toast.makeText(
                    panelContext,
                    localized("设备未连接", "Device not connected"),
                    android.widget.Toast.LENGTH_SHORT
                ).show()
            } else {
                coroutineScope.launch {
                    val currentMode = isAlwaysLateral ?: carrotParamClient
                        .getParams("AlwaysLateral")
                        .getOrNull()
                        ?.get("AlwaysLateral")
                        ?.let(::parseExperimentalMode)
                        ?: false
                    val targetMode = !currentMode
                    val result = carrotParamClient.setAlwaysLateral(targetMode)
                    if (result.isSuccess) {
                        isAlwaysLateral = targetMode
                        android.widget.Toast.makeText(
                            panelContext,
                            if (targetMode) localized("已开启横向控制", "AlwaysLateral ON")
                            else localized("已关闭横向控制", "AlwaysLateral OFF"),
                            android.widget.Toast.LENGTH_SHORT
                        ).show()
                    } else {
                        android.widget.Toast.makeText(
                            panelContext,
                            result.exceptionOrNull()?.message
                                ?: localized("切换失败", "Switch failed"),
                            android.widget.Toast.LENGTH_SHORT
                        ).show()
                    }
                }
            }
        }

        val onShareDataClick: () -> Unit = {
            if (carrotParamClient == null) {
                android.widget.Toast.makeText(
                    panelContext,
                    localized("设备未连接", "Device not connected"),
                    android.widget.Toast.LENGTH_SHORT
                ).show()
            } else {
                coroutineScope.launch {
                    val currentMode = isShareDataEnabled ?: carrotParamClient
                        .getParams("ShareData")
                        .getOrNull()
                        ?.get("ShareData")
                        ?.let(::parseExperimentalMode)
                        ?: false
                    val targetMode = !currentMode
                    val result = carrotParamClient.setParam("ShareData", if (targetMode) 1 else 0)
                    if (result.isSuccess) {
                        isShareDataEnabled = targetMode
                        android.widget.Toast.makeText(
                            panelContext,
                            if (targetMode) localized("数据分发已开启", "Share ON")
                            else localized("数据分发已关闭", "Share OFF"),
                            android.widget.Toast.LENGTH_SHORT
                        ).show()
                    } else {
                        android.widget.Toast.makeText(
                            panelContext,
                            result.exceptionOrNull()?.message
                                ?: localized("切换失败", "Switch failed"),
                            android.widget.Toast.LENGTH_SHORT
                        ).show()
                    }
                }
            }
        }

        val selectedDeviceIP by core.selectedCameraHost.collectAsState()
        HomeDashboardTabs(footer = { section ->
            // 实时页没有底部标题条：路口引导条在驾驶卡正下方，导航数据在卡片下方滚动区。
            // 控制页（唯一非实时页）：显示大标题、副标题和「手动」标记。
            if (section != 0) {
                com.jixiexiaoge.drivingassist.ui.components.SectionTitleFooter(section)
            }
        }) { section ->
            when (section) {
                // 实时页：前视画面 + 原看板全部内容（原「看板」与「AR」两页合并）
                0 -> {
                    val cameraHost by core.selectedCameraHost.collectAsState()
                    // 8.3.2：还没发现设备时，把「正在扫哪个网段 / 扫了几台」直接显示在视频空态里。
                    // 出问题时能一眼看出是没广播、扫错网段，还是连不上。
                    // 赋值在 while(isActive) 体内，lint 的 ProduceStateDoesNotAssignValue
                    // 只认顶层赋值语句，这里是有意抑制（逻辑分支太长，不重复一遍）。
                    @Suppress("ProduceStateDoesNotAssignValue")
                    val discoveryHint by produceState<String?>(initialValue = null, cameraHost) {
                        while (isActive) {
                            value = if (cameraHost.isNullOrBlank()) {
                            runCatching {
                                val d = core.networkManager?.discoveryDiagnostics()
                                    ?: return@runCatching "网络客户端未初始化"
                                // 错误排在最前面：这一行会被 maxLines 截断，报错不能被截掉
                                val head = if (d.lastError.isNotEmpty()) {
                                    "⚠ ${d.lastError} · "
                                } else {
                                    ""
                                } + "扫描${if (d.scanStarted) "已启动" else "未启动"}" +
                                    " · ${d.scanRounds}轮 · 广播 ${d.broadcastPackets}" +
                                    " · 本机 ${d.phoneIps.joinToString(",").ifEmpty { "无地址" }}"
                                val subnets = if (d.scanPrefixes.isEmpty()) {
                                    " · 网段为空"
                                } else {
                                    " · ${d.scanPrefixes.joinToString(",")}.0/24 · 探测 ${d.scanProbes}"
                                }
                                "$head$subnets"
                            }.getOrElse { "诊断异常: ${it.message}" }
                        } else null
                        delay(1_000L)
                        }
                    }
                    com.jixiexiaoge.drivingassist.ui.components.RealTimePage(
                        host = cameraHost,
                        snapshot = core.dashboardSnapshot.value,
                        // 设备搜索/发现结果并进画面区：连上就出画面，只发现设备就显示 IP + 连接状态，
                        // 5001 不通则提示开启视频流功能
                        deviceIp = selectedDeviceIP,
                        connection = commaConnectionDisplay,
                        onConnectionClick = onShow7706Debug,
                    discoveryHint = discoveryHint,
                        // 驾驶模式（只读）：Auto / EAP / NOA / NOO，显示在驾驶卡右上角
                        drivingMode = effectiveDrivingMode,
                        // 车道宽度：左 | 本 | 右（米）。来自 7000 modelV2 的 4 条 laneLines，
                        // 在前视 5 米处插值后相邻差分；不是高德广播给的值。
                        laneGeometry = core.laneGeometry.value,
                        laneFusion = core.laneFusion.value,
                        // 路口车道引导条：紧跟驾驶卡正下方居中。
                        // 沿用原来的取值优先级：导航短暂断流时优先显示保留的本路线最近图示，避免图标闪没。
                        junction = amapDisplay.displayJunction ?: amapDisplay.junction,
                        // 原导航页的全部内容（7.9.44 并入实时页滚动区）
                        amap = amapDisplay,
                        vehicle = telemetryDisplay,
                        delivery = core.naviDeliveryState,
                        routeReady = core.routeTaskState.ready,
                        onAboutClick = { aboutOpenTrigger++ },
                        // 左半屏内嵌浏览器：没开高德 / 投屏未开启时点提示跳投屏页
                        )
                }
                // 控制页（唯一可写页）；索引越界也落到这里
                else -> Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    MainActivityUIComponents.AdvancedFunctionsContent(
                        onSendCommand = onSendCommand,
                        onSendRoadLimitSpeed = onSendRoadLimitSpeed,
                        onLaunchAmap = onLaunchAmap,
                        onSendNavConfirmation = onSendNavConfirmation,
                        onPageChange = onPageChange,
                        isOpenpilotActive = carrotManFields.active,
                        carrotManFields = carrotManFields,
                        networkManager = try { core.networkManager } catch (_: Exception) { null },
                        context = panelContext,
                        onShow7706Debug = onShow7706Debug,
                        onAlwaysLateralClick = { onAlwaysLateralClick() },
                        isAlwaysLateralEnabled = isAlwaysLateral,
                        onShareDataClick = { onShareDataClick() },
                        isShareDataEnabled = isShareDataEnabled,
                        trafficLightState = carrotManFields.trafficLightState,
                        trafficLightCountdown = carrotManFields.trafficLightCountdown,
                        trafficLightDir = carrotManFields.amap_traffic_light_dir,
                        automationObservation = core.automationObservation,
                    )
                }
            }
        }
        MainActivityUIComponents.AboutDialog(panelContext, aboutOpenTrigger)
    }

    /** 目的地滑块触发阈值（回家/公司） */
    private val destinationTriggerThreshold = 0.85f

    @Composable
    @OptIn(ExperimentalMaterial3Api::class)
    private fun MapDestinationCapsule(
        onNavigateHome: () -> DestinationNavigationResult,
        onNavigateCompany: () -> DestinationNavigationResult,
    ) {
        val context = LocalContext.current
        var sliderPosition by remember { mutableStateOf(0f) }
        var destination by remember { mutableStateOf<String?>(null) }

        LaunchedEffect(destination) {
            if (destination != null) {
                delay(850)
                sliderPosition = 0f
                destination = null
            }
        }

        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(68.dp)
                .border(1.dp, Color.White.copy(alpha = 0.12f), RoundedCornerShape(34.dp))
                .clip(RoundedCornerShape(34.dp))
                .background(
                    Brush.horizontalGradient(
                        listOf(Color(0xFF1E3A8A), Color(0xFF2563EB), Color(0xFF38BDF8)),
                    ),
                ),
            contentAlignment = Alignment.Center,
        ) {
            Row(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 22.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("回家", color = OnBrand, fontSize = 22.sp, fontWeight = FontWeight.Bold)
                Spacer(Modifier.weight(1f))
                Text("公司", color = OnBrand, fontSize = 22.sp, fontWeight = FontWeight.Bold)
            }
            Slider(
                value = sliderPosition,
                onValueChange = { value ->
                    if (destination == null) sliderPosition = value
                },
                onValueChangeFinished = {
                    if (destination != null) return@Slider
                    destination = when {
                        sliderPosition <= -destinationTriggerThreshold -> {
                            val result = onNavigateHome()
                            android.widget.Toast.makeText(
                                context,
                                result.message,
                                android.widget.Toast.LENGTH_SHORT,
                            ).show()
                            if (result.accepted) "home" else {
                                sliderPosition = 0f
                                null
                            }
                        }
                        sliderPosition >= destinationTriggerThreshold -> {
                            val result = onNavigateCompany()
                            android.widget.Toast.makeText(
                                context,
                                result.message,
                                android.widget.Toast.LENGTH_SHORT,
                            ).show()
                            if (result.accepted) "company" else {
                                sliderPosition = 0f
                                null
                            }
                        }
                        else -> {
                            sliderPosition = 0f
                            null
                        }
                    }
                },
                valueRange = -1f..1f,
                enabled = destination == null,
                colors = SliderDefaults.colors(
                    thumbColor = Color(0xFFF4F7FB),
                    activeTrackColor = Color.Transparent,
                    inactiveTrackColor = Color.Transparent,
                    disabledThumbColor = Color(0xFFF4F7FB),
                    disabledActiveTrackColor = Color.Transparent,
                    disabledInactiveTrackColor = Color.Transparent,
                ),
                thumb = {
                    Box(
                        modifier = Modifier
                            .width(88.dp)
                            .height(44.dp)
                            .border(1.dp, Color.White.copy(alpha = 0.16f), RoundedCornerShape(22.dp))
                            .clip(RoundedCornerShape(22.dp))
                            .background(
                                Brush.horizontalGradient(
                                    listOf(Color(0xFF3B82F6), Color(0xFF1D4ED8)),
                                ),
                            ),
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(
                            "导航",
                            color = TextPrimary,
                            fontSize = 15.sp,
                            fontWeight = FontWeight.SemiBold,
                        )
                    }
                },
                modifier = Modifier.fillMaxWidth(),
            )
        }
    }

    @Composable
    private fun SmartSpeedPage(
        onBack: () -> Unit,
        fields: CarrotManFields,
    ) {
        val context = LocalContext.current
        val prefs = remember { context.getSharedPreferences("CarrotAmap", Context.MODE_PRIVATE) }
        var enabled by remember { mutableStateOf(prefs.getBoolean(SmartSpeedLimiter.KEY_ENABLED, true)) }
        var rampProtection by remember { mutableStateOf(prefs.getBoolean(SmartSpeedLimiter.KEY_RAMP_PROTECTION, true)) }
        var accelMainline by remember { mutableStateOf(prefs.getBoolean(SmartSpeedLimiter.KEY_ACCEL_TO_MAINLINE, true)) }
        var decelRamp by remember { mutableStateOf(prefs.getBoolean(SmartSpeedLimiter.KEY_DECEL_TO_RAMP, true)) }
        var curveHold by remember { mutableStateOf(prefs.getBoolean(SmartSpeedLimiter.KEY_CURVE_HOLD_ENABLED, true)) }

        fun save() {
            prefs.edit()
                .putBoolean(SmartSpeedLimiter.KEY_ENABLED, enabled)
                .putBoolean(SmartSpeedLimiter.KEY_RAMP_PROTECTION, rampProtection)
                .putBoolean(SmartSpeedLimiter.KEY_ACCEL_TO_MAINLINE, accelMainline)
                .putBoolean(SmartSpeedLimiter.KEY_DECEL_TO_RAMP, decelRamp)
                .putBoolean(SmartSpeedLimiter.KEY_CURVE_HOLD_ENABLED, curveHold)
                .apply()
        }

        LaunchedEffect(enabled, rampProtection, accelMainline, decelRamp, curveHold) {
            save()
        }

        val lastOutput = prefs.getInt(SmartSpeedLimiter.KEY_LAST_OUTPUT, fields.nRoadLimitSpeed)
        val lastTarget = prefs.getInt(SmartSpeedLimiter.KEY_LAST_TARGET, 0)
        val lastType = prefs.getString(SmartSpeedLimiter.KEY_LAST_TYPE, SmartSpeedTransitionType.None.name) ?: SmartSpeedTransitionType.None.name
        val lastDistance = prefs.getInt(SmartSpeedLimiter.KEY_LAST_DISTANCE, fields.nTBTDist)
        val lastReason = prefs.getString(SmartSpeedLimiter.KEY_LAST_REASON, "等待导航数据") ?: "等待导航数据"
        val waiting = lastType in setOf(
            SmartSpeedTransitionType.CurveHold.name,
            SmartSpeedTransitionType.WaitingForMainlineLimit.name,
            SmartSpeedTransitionType.WaitingForConfirmation.name,
            SmartSpeedTransitionType.RecoveryTimedOut.name,
        )
        val cardColor = if (waiting) Color(0xFF374151) else Primary

        AdaptivePageColumn(modifier = Modifier.background(Surface900)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically
            ) {
                IconButton(onClick = onBack) {
                    Icon(Icons.Default.ArrowBack, contentDescription = "返回", tint = TextPrimary)
                }
                Column(modifier = Modifier.weight(1f)) {
                    Text("智能调速", color = TextPrimary, fontSize = 22.sp, fontWeight = FontWeight.Bold)
                    Text("NOA 路线速度预调，仅高速 / 高架 / 快速路场景启用", color = TextSecondary, fontSize = 12.sp)
                }
                Switch(checked = enabled, onCheckedChange = { enabled = it })
            }

            Card(
                colors = CardDefaults.cardColors(containerColor = cardColor),
                shape = RoundedCornerShape(18.dp),
                modifier = Modifier.fillMaxWidth()
            ) {
                Column(modifier = Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    Text("最近策略输出 ${lastOutput.takeIf { it > 0 } ?: fields.nRoadLimitSpeed} km/h", color = OnBrand, fontSize = 28.sp, fontWeight = FontWeight.ExtraBold)
                    FlowRow(
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                        verticalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        SmartSpeedStatusChip("状态", smartSpeedTypeText(lastType))
                        SmartSpeedStatusChip("路线目标", if (lastTarget > 0) "${lastTarget}km/h" else "--")
                        SmartSpeedStatusChip("距离", if (lastDistance > 0) "${lastDistance}m" else "--")
                    }
                    Text(lastReason, color = OnBrand.copy(alpha = 0.86f), fontSize = 13.sp)
                    Text(
                        "地图原始限速在看板独立显示；有效策略通过 7714 导航限速发送。此处为最近计算记录，非实时发送、车端采用或执行确认。",
                        color = OnBrand.copy(alpha = 0.86f), fontSize = 12.sp,
                    )
                }
            }

            AdaptiveCardGrid {
                Card(
                    colors = CardDefaults.cardColors(containerColor = Surface800.copy(alpha = 0.72f)),
                    shape = RoundedCornerShape(16.dp),
                    modifier = Modifier.weight(1f).fillMaxWidth()
                ) {
                    Column(modifier = Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text("固定规则", color = TextPrimary, fontWeight = FontWeight.Bold, fontSize = 15.sp)
                        SmartSpeedInfoRow("下匝道", "距匝道 800m 开始，每档降 10km/h，按剩余距离分配档位，提前降到 60km/h；首次发现不足 800m 时按实际距离安排。")
                        SmartSpeedInfoRow("上匝道", "等待真实主路限速稳定3秒，不猜80/120；恢复不依赖旧动作距离，每3秒最多升一档10km/h，90秒未完成需人工确认。")
                        SmartSpeedInfoRow("弯道", "7000曲率与车速须新鲜配对，持续变缓2秒才允许恢复；缺失或弯道暂停升速。此为工程门槛，不证明已汇入或安全速度。")
                        SmartSpeedInfoRow("巡航", "既有自动提速仍以原始道路限速为上限；恢复期间另受策略限制，每档等待7000设定回报，手动调速取消本轮自动巡航提速。策略完成不等于车辆执行。")
                    }
                }

                Card(
                    colors = CardDefaults.cardColors(containerColor = Surface800.copy(alpha = 0.72f)),
                    shape = RoundedCornerShape(16.dp),
                    modifier = Modifier.weight(1f).fillMaxWidth()
                ) {
                    Column(modifier = Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        SmartSpeedSwitchRow("匝道 40→60 保护", "高德在高速/快速路给 40 时，输出匝道基准限速", rampProtection) { rampProtection = it }
                        SmartSpeedSwitchRow("匝道进入主路升速", "真实限速确认后按时间分档恢复，不依赖旧匝道距离", accelMainline) { accelMainline = it }
                        SmartSpeedSwitchRow("主路进入匝道降速", "准备下匝道/出口时提前平滑降到匝道限速", decelRamp) { decelRamp = it }
                        SmartSpeedSwitchRow("弯道暂停升速", "曲率缺失、过期或弯道时暂停汇入升速；下匝道降速不受影响", curveHold) { curveHold = it }
                    }
                }
            }
        }
    }

    @Composable
    private fun SmartSpeedStatusChip(label: String, value: String) {
        Surface(color = OnBrand.copy(alpha = 0.16f), shape = RoundedCornerShape(10.dp)) {
            Text("$label $value", color = OnBrand, fontSize = 11.sp, modifier = Modifier.padding(horizontal = 8.dp, vertical = 5.dp))
        }
    }

    @Composable
    private fun SmartSpeedSwitchRow(title: String, subtitle: String, checked: Boolean, onCheckedChange: (Boolean) -> Unit) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp)
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(title, color = TextPrimary, fontSize = 14.sp, fontWeight = FontWeight.SemiBold)
                Text(subtitle, color = TextSecondary, fontSize = 11.sp)
            }
            Switch(checked = checked, onCheckedChange = onCheckedChange)
        }
    }

    @Composable
    private fun SmartSpeedInfoRow(title: String, detail: String) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalAlignment = Alignment.Top,
        ) {
            Surface(color = Primary, shape = RoundedCornerShape(8.dp)) {
                Text(title, color = OnBrand, fontSize = 11.sp, fontWeight = FontWeight.Bold, modifier = Modifier.padding(horizontal = 8.dp, vertical = 5.dp))
            }
            Text(detail, color = TextSecondary, fontSize = 12.sp, lineHeight = 16.sp, modifier = Modifier.weight(1f))
        }
    }

    private fun smartSpeedTypeText(type: String): String = when (type) {
        SmartSpeedTransitionType.RampProtection.name -> "匝道保护"
        SmartSpeedTransitionType.RampToMainline.name -> "匝道进主路"
        SmartSpeedTransitionType.MainlineToRamp.name -> "主路进匝道"
        SmartSpeedTransitionType.CurveHold.name -> "曲率待确认"
        SmartSpeedTransitionType.WaitingForMainlineLimit.name -> "等待主路限速"
        SmartSpeedTransitionType.WaitingForConfirmation.name -> "等待限速稳定"
        SmartSpeedTransitionType.RecoveryTimedOut.name -> "恢复超时"
        else -> "跟随导航"
    }

    @Composable
    private fun SecondaryPageHeader(title: String, subtitle: String) {
        Surface(color = Surface800, modifier = Modifier.fillMaxWidth()) {
            Row(
                Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                IconButton(onClick = { core.currentPage = Page.CpFunctions }) {
                    Icon(CustomIcons.ArrowBack, localized("返回首页", "Back to dashboard"), tint = TextPrimary)
                }
                Column(Modifier.weight(1f)) {
                    Text(title, color = TextPrimary, fontSize = 20.sp, fontWeight = FontWeight.Bold)
                    Text(subtitle, color = TextSecondary, fontSize = 12.sp)
                }
            }
        }
    }
}
