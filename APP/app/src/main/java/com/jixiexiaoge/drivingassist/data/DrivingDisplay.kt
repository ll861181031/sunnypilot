@file:Suppress("MagicNumber")

package com.jixiexiaoge.drivingassist.data

/**
 * 周围车辆，供 3D 场景渲染用。
 *
 * 坐标系沿用 openpilot：forwardM 是纵向距离（前正），lateralM 是横向偏移（**左正**）。
 * 场景渲染里 x 轴是"右正"，所以绘制时 x 要取 -lateralM，别直接当右正用。
 */
internal data class LeadCar(
    val forwardM: Float,
    val lateralM: Float,
    val relativeKph: Float,
    /** 该车正在刹车（aLead 明显为负），3D 场景据此点亮它的尾灯 */
    val braking: Boolean = false,
)

internal data class DrivingDisplay(
    val speedKph: Float? = null,
    val gear: String? = null,
    val steeringDeg: Float? = null,
    val cruiseKph: Float? = null,
    val adoptedLimitKph: Int? = null,
    val suggestedKph: Int? = null,
    val brakeLights: Boolean? = null,
    /** 近光灯（实车 STALKS.HEADLIGHT） */
    val headlight: Boolean? = null,
    /** 远光灯（实车 STALKS.HIGH_BEAM） */
    val highBeam: Boolean? = null,
    /** 自动大灯开关（实车 STALKS.AUTO_HEADLIGHT） */
    val autoHeadlight: Boolean? = null,
    /** 电子驻车制动（实车 REAR_BCM_STATUS.EPB_ACTIVE） */
    val parkingBrake: Boolean? = null,
    val leftBlinker: Boolean? = null,
    val rightBlinker: Boolean? = null,
    /** 左前门（实车 BCM_DOOR_STATUS.FRONT_LEFT_DOOR） */
    val doorFrontLeft: Boolean? = null,
    /** 右前门（实车 BCM_DOOR_STATUS.FRONT_RIGHT_DOOR） */
    val doorFrontRight: Boolean? = null,
    /** 左后门（实车 BCM_DOOR_STATUS.BACK_LEFT_DOOR） */
    val doorRearLeft: Boolean? = null,
    /** 右后门（实车 BCM_DOOR_STATUS.BACK_RIGHT_DOOR） */
    val doorRearRight: Boolean? = null,
    /** 有任一车门打开（实车 compact 给整车级状态） */
    val doorOpen: Boolean? = null,
    /** 后备箱/尾门打开 */
    val tailgateOpen: Boolean? = null,
    val leftBlindspot: Boolean? = null,
    val rightBlindspot: Boolean? = null,
    val lateralActive: Boolean? = null,
    val longitudinalActive: Boolean? = null,
    val laneChange: ReceivedLaneChange? = null,
    val alert: ReceivedAlert? = null,
    val speedEvent: ReceivedSpeedEvent? = null,
    /** 周围车辆（radarState 的 leadOne/leadTwo/leadLeft/leadRight），3D 场景画他车用 */
    val leadCars: List<LeadCar> = emptyList(),
) {
    val parked: Boolean get() = gear == "park"
}

internal fun CompactTelemetry?.drivingDisplay(display: CarrotDisplaySnapshot): DrivingDisplay {
    val car = this?.fresh("carState")?.frame?.values.orEmpty()
    fun number(key: String, range: ClosedFloatingPointRange<Float>): Float? =
        (car[key] as? Number)?.toFloat()?.takeIf { it.isFinite() && it in range }
    val leads = listOf("leadOne", "leadTwo", "leadLeft", "leadRight")
        .mapNotNull { name ->
            this?.radarLead(name)
                ?.takeIf { it.distanceM in 1f..150f && kotlin.math.abs(it.lateralM) <= 8f }
                ?.let {
                    LeadCar(
                        it.distanceM, it.lateralM, it.relativeSpeedMps * 3.6f,
                        // −0.8 m/s² 是个保守阈值：正常松电门滑行的减速度约 −0.3~−0.6，
                        // 真实踩刹车通常 −2 以上。取在两者之间，避免把滑行误判成刹车。
                        braking = (it.aLeadMps2 ?: 0f) < -0.8f,
                    )
                }
        }
    val out = DrivingDisplay(
        speedKph = number("vEgo", 0f..100f)?.times(3.6f),
        gear = car["gearShifter"] as? String,
        steeringDeg = number("steeringAngleDeg", -1080f..1080f),
        cruiseKph = number("vCruiseCluster", 0.1f..249f),
        adoptedLimitKph = display.adoptedLimitKph,
        suggestedKph = display.desiredSpeedKph,
        brakeLights = car["brakeLights"] as? Boolean,
        headlight = car["headlight"] as? Boolean,
        highBeam = car["highBeam"] as? Boolean,
        autoHeadlight = car["autoHeadlight"] as? Boolean,
        parkingBrake = car["parkingBrake"] as? Boolean,
        leftBlinker = car["leftBlinker"] as? Boolean,
        rightBlinker = car["rightBlinker"] as? Boolean,
        doorFrontLeft = car["doorFrontLeft"] as? Boolean,
        doorFrontRight = car["doorFrontRight"] as? Boolean,
        doorRearLeft = car["doorRearLeft"] as? Boolean,
        doorRearRight = car["doorRearRight"] as? Boolean,
        doorOpen = car["doorOpen"] as? Boolean,
        tailgateOpen = car["tailgateOpen"] as? Boolean,
        leftBlindspot = display.leftBlindspot,
        rightBlindspot = display.rightBlindspot,
        lateralActive = display.latActive,
        longitudinalActive = display.longActive,
        laneChange = display.laneChange,
        alert = display.alert,
        speedEvent = display.events.filter { it.type in setOf(0, 1, 2, 7, 8, 75, 76) && it.distanceM > 0 }
            .minByOrNull { it.distanceM },
        leadCars = leads,
    )
    return out
}
