@file:Suppress("MagicNumber", "FunctionNaming")

package com.jixiexiaoge.drivingassist.ui.components

import android.content.Context
import android.graphics.BitmapFactory
import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.opengl.GLUtils
import android.opengl.Matrix
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.Modifier
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.Text
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.jixiexiaoge.drivingassist.data.DashboardRoad
import com.jixiexiaoge.drivingassist.data.DrivingDisplay
import com.jixiexiaoge.drivingassist.data.LeadCar
import javax.microedition.khronos.egl.EGL10
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.egl.EGLDisplay
import javax.microedition.khronos.opengles.GL10
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer
import kotlin.math.cos
import kotlin.math.max
import kotlin.math.sin

/**
 * 车辆场景 3D 实时渲染（真实光照版）。
 *
 * 与上一版的区别：上一版是卡通着色（只区分顶面/侧面），这一版换成真正的
 * Blinn-Phong 光照 + 半球环境光，配合分层车身、接地阴影、车道线，让它看起来
 * 像一条真实的夜间道路，而不是低多边形示意图。
 *
 * 渲染内容：
 *   1. **光照**：方向光（车顶左上方）+ 半球环境光（天空色 ↔ 地面反光）+ 高光，
 *      车漆和玻璃用不同的高光强度，玻璃更亮更锐
 *   2. **车身**：6 层几何拼出轮廓 —— 底盘、车身、车顶、玻璃、前后灯、4 个轮子
 *   3. **车道线**：车端 `road.curves` 有数据就用真实点串；没有数据时画默认三车道
 *      （左实线 / 中虚 / 右实线），保证画面里始终是一条路
 *   4. **接地阴影**：车底一块渐隐的暗色椭圆，这是「车趴在地上」的关键
 *   5. **环境**：路两侧球冠树（树干 + 三层球体）+ 路灯
 *   6. **发光**：车底青色光环 + 车前光带，加法混合
 *   7. **雾**：55m 起渐隐，165m 外完全融进背景
 *
 * 数据来源：车端 7000 的 `DashboardRoad.curves` 与 `DrivingDisplay`。
 * 姿态角（侧倾/俯仰）是推算的，不是车辆姿态传感器实测值。
 */
@Composable
internal fun VehicleScene3D(
    driving: DrivingDisplay?,
    road: DashboardRoad?,
    modifier: Modifier = Modifier,
) {
    val holder = remember { SceneData() }
    SideEffect { holder.update(driving, road) }

    // 车模解析（宋 PLUS V2：11.14MB / 53.8 万顶点）放 IO 线程；GL 线程只上传 VBO 和画，
    // 卡在 GL 线程会被容器判无响应直接 FREEZE 整个进程。
    val ctx = LocalContext.current.applicationContext
    LaunchedEffect(Unit) {
        CrashLog.install(ctx)
        if (holder.carData.isEmpty()) {
            holder.carData = kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.IO) { CarAssets.load(ctx) }
        }
    }

    // 容器的 chromium WebView 合成器损坏：GPU 内容永远到不了窗口（PC Chrome 和
    // 鸿蒙原生浏览器都正常，唯独容器 WebView 全黑，软/硬件模式均试过）。
    // GLSurfaceView 有自己的 EGL surface，在这个容器里一直可靠出图，用它渲染。
    AndroidView(
        factory = { ctx: Context ->
            GLSurfaceView(ctx).apply {
                setEGLContextClientVersion(2)
                preserveEGLContextOnPause = true
                // 4x MSAA：车模是"白车漆 + 深色玻璃/包围"的强对比几何，没有抗锯齿时
                // 所有斜边都是阶梯状锯齿，缩小到卡片尺寸就像一片黑白碎片。
                setEGLConfigChooser(MsaaConfigChooser(4))
                setRenderer(SceneRenderer(holder, ctx.applicationContext))
                renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY
            }
        },
        modifier = modifier,
        onRelease = { view ->
            view.queueEvent { SceneRenderer.releaseAll() }
            view.onPause()
        },
    )
}


internal class SceneData {
    @Volatile private var speedKph = 0f
    @Volatile private var steeringDeg = 0f
    @Volatile private var braking = false
    @Volatile private var gear: String? = null
    @Volatile private var doorOpen: Boolean? = null
    @Volatile private var tailgateOpen: Boolean? = null
    @Volatile private var doorFrontLeft: Boolean? = null
    @Volatile private var doorFrontRight: Boolean? = null
    @Volatile private var doorRearLeft: Boolean? = null
    @Volatile private var doorRearRight: Boolean? = null
    @Volatile private var leftBlinker: Boolean? = null
    @Volatile private var rightBlinker: Boolean? = null
    @Volatile private var brakeLights: Boolean? = null
    @Volatile private var headlight: Boolean? = null
    @Volatile private var highBeam: Boolean? = null
    @Volatile private var autoHeadlight: Boolean? = null
    @Volatile private var parkingBrake: Boolean? = null
    @Volatile private var curves: List<FloatArray> = emptyList()
    /** 周围车辆（radarState）。空表示没探测到目标。 */
    @Volatile private var leads: List<LeadCar> = emptyList()
    @Volatile private var revision = 0

    /** 车模纯数据（IO 线程填，GL 线程读后上传 VBO） */
    @Volatile var carData: List<CarData> = emptyList()

    fun update(driving: DrivingDisplay?, road: DashboardRoad?) {
        speedKph = driving?.speedKph ?: 0f
        steeringDeg = driving?.steeringDeg ?: 0f
        braking = driving?.brakeLights == true
        gear = driving?.gear
        doorOpen = driving?.doorOpen
        tailgateOpen = driving?.tailgateOpen
        doorFrontLeft = driving?.doorFrontLeft
        doorFrontRight = driving?.doorFrontRight
        doorRearLeft = driving?.doorRearLeft
        doorRearRight = driving?.doorRearRight
        leftBlinker = driving?.leftBlinker
        rightBlinker = driving?.rightBlinker
        brakeLights = driving?.brakeLights
        headlight = driving?.headlight
        highBeam = driving?.highBeam
        autoHeadlight = driving?.autoHeadlight
        parkingBrake = driving?.parkingBrake
        curves = road?.curves?.map { c ->
            val out = FloatArray(c.points.size * 3)
            c.points.forEachIndexed { i, p ->
                out[i * 3] = p.rightM
                out[i * 3 + 1] = 0f
                out[i * 3 + 2] = p.forwardM
            }
            out
        } ?: emptyList()
        leads = driving?.leadCars ?: emptyList()
        revision++
    }

    fun read(out: Snapshot) {
        out.speedKph = speedKph
        out.steeringDeg = steeringDeg
        out.braking = braking
        out.gear = gear
        out.doorOpen = doorOpen
        out.tailgateOpen = tailgateOpen
        out.doorFrontLeft = doorFrontLeft
        out.doorFrontRight = doorFrontRight
        out.doorRearLeft = doorRearLeft
        out.doorRearRight = doorRearRight
        out.leftBlinker = leftBlinker
        out.rightBlinker = rightBlinker
        out.brakeLights = brakeLights
        out.headlight = headlight
        out.highBeam = highBeam
        out.autoHeadlight = autoHeadlight
        out.parkingBrake = parkingBrake
        out.curves = curves
        out.leads = leads
        out.revision = revision
    }

    class Snapshot {
        var speedKph = 0f
        var steeringDeg = 0f
        var braking = false
        var gear: String? = null
        var doorOpen: Boolean? = null
        var tailgateOpen: Boolean? = null
        var doorFrontLeft: Boolean? = null
        var doorFrontRight: Boolean? = null
        var doorRearLeft: Boolean? = null
        var doorRearRight: Boolean? = null
        var leftBlinker: Boolean? = null
        var rightBlinker: Boolean? = null
        var brakeLights: Boolean? = null
        var headlight: Boolean? = null
        var highBeam: Boolean? = null
        var autoHeadlight: Boolean? = null
        var parkingBrake: Boolean? = null
        var curves: List<FloatArray> = emptyList()
        var leads: List<LeadCar> = emptyList()
        var revision = -1
    }
}

private const val FPV = 8   // pos(3) + normal(3) + material(2: spec, shininess/64)
private const val STRIDE = FPV * 4

// ── 场景配色（夜间沥青路的冷调）──────────────────────────────
// 白天：雾色/背景是晴天的浅蓝，路面是中性灰沥青（比夜里亮一大截）
private const val BG_R = 0.62f
private const val BG_G = 0.78f
private const val BG_B = 0.93f
private const val ROAD_R = 0.40f
private const val ROAD_G = 0.42f
private const val ROAD_B = 0.45f
// 半球环境光的上半球（天空）和下半天（地面反光）
private const val SKY_R = 0.72f
private const val SKY_G = 0.86f
private const val SKY_B = 1.00f
// 发光在白天是被日光压掉的，只留一点点提示色
private const val GLOW_R = 0.30f
private const val GLOW_G = 0.72f
private const val GLOW_B = 0.95f

private const val SCENE_VS = """
uniform mat4 uMvp;
uniform mat4 uModel;
uniform mat3 uNormalMat;    // 门/盖绕铰链转过后法线必须一起转，否则光照错位
uniform float uNormalOffset;
attribute vec3 aPos;
attribute vec3 aNormal;
attribute vec2 aMat;        // x = 高光强度, y = 光泽度/64
attribute vec2 aUv;
varying vec3 vNormal;
varying vec3 vWorld;
varying vec2 vMat;
varying vec2 vUv;
void main() {
    vNormal = uNormalMat * aNormal;
    vMat = aMat;
    vUv = aUv;
    vec3 position = aPos + aNormal * uNormalOffset;
    vec4 w = uModel * vec4(position, 1.0);
    vWorld = w.xyz;
    gl_Position = uMvp * vec4(position, 1.0);
}
"""

/**
 * Blinn-Phong + 半球环境光 + 距离雾。
 * 环境光不是常数：朝上的面偏天空色、朝下的面偏地面反光，这样车身下部会自然变暗，
 * 比单纯给一个 ambient 值立体得多。
 */
private const val SCENE_FS = """
precision mediump float;
uniform vec3 uColor;
uniform vec3 uFogColor;
uniform vec3 uSkyColor;
uniform vec3 uGroundColor;
uniform vec3 uLightDir;     // 指向光源
uniform vec3 uViewPos;
uniform float uAmbient;
uniform float uDiffuse;
uniform float uAlpha;
uniform vec3 uLamp;         // 灯的自发光色（0 = 不发光）
uniform sampler2D uTexture;
uniform float uUseTexture;
uniform int uAlphaMode;     // 0 = OPAQUE, 1 = BLEND, 2 = MASK
uniform float uAlphaCutoff;
uniform float uLampOverlay; // emissive overlay does not inherit the translucent lens opacity
varying vec3 vNormal;
varying vec3 vWorld;
varying vec2 vMat;
varying vec2 vUv;
void main() {
    vec4 texel = vec4(1.0);
    if (uUseTexture > 0.5) texel = texture2D(uTexture, vUv);
    if (uLampOverlay > 0.5) {
        if (uAlphaMode == 2 && texel.a < uAlphaCutoff) discard;
        gl_FragColor = vec4(uLamp * texel.rgb * 1.75, texel.a);
        return;
    }
    float alpha = uAlpha * texel.a;
    if (uAlphaMode == 2 && alpha < uAlphaCutoff) discard;
    if (uAlphaMode != 1) alpha = 1.0;
    vec3 baseColor = uColor * texel.rgb;
    vec3 N0 = normalize(vNormal);
    // NaN / 零长度兜底：normalize(vec3(0)) 得到 NaN，法线一坏整车会渲染成纯黑。
    // NaN != NaN 恒真，所以 !(x > 0.25) 对 NaN 也成立（用 == 比较同理）。
    if (!(dot(N0, N0) > 0.25)) N0 = vec3(0.0, 1.0, 0.0);
    vec3 L = normalize(uLightDir);
    vec3 V = normalize(uViewPos - vWorld);
    if (!(dot(V, V) > 0.25)) V = vec3(0.0, 0.0, 1.0);
    vec3 N = N0;
    // 双面光照：车模不同子网格的三角形环绕方向不一致（同组里混有反向面），
    // 法线背向视点的面按原方向算只剩地面环境光，整块发黑；这里统一翻向视点。
    if (dot(N, V) < 0.0) N = -N;
    vec3 H = normalize(L + V);

    // 半球环境光：N.y 决定天空/地面占比
    vec3 ambient = mix(uGroundColor, uSkyColor, 0.5 + 0.5 * N.y) * uAmbient;
    // 漫反射
    float ndl = max(dot(N, L), 0.0);
    vec3 diffuse = baseColor * ndl * uDiffuse;
    // 高光（Blinn-Phong）
    float specPow = max(vMat.y * 64.0, 1.0);
    float spec = pow(max(dot(N, H), 0.0), specPow) * vMat.x;

    // 菲涅尔：掠射角把天空色反射进来，车漆/玻璃（vMat.x 高）才有，轮胎内饰没有。
    // 这是对 carcard 里 HDR 环境反射的 ES2 近似。
    float fres = pow(1.0 - max(dot(N, V), 0.0), 3.0);
    vec3 color = baseColor * ambient + diffuse + vec3(spec)
        + fres * uSkyColor * 0.22 * smoothstep(0.30, 0.90, vMat.x);
    // 距离雾
    float dist = length(vWorld.xz - uViewPos.xz);
    float fog = smoothstep(95.0, 260.0, dist);   // 白天能见度比夜里高
    color = mix(color, uFogColor, fog);
    // 灯具自发光：直接叠加，过 1.0 由输出钳位成亮斑
    color += uLamp * texel.rgb * 1.75;
    // 输出前最终兜底：任何一路 NaN（法线/光线/雾）都会让整块渲染成纯黑，
    // 这里把它拉回中性灰，宁可颜色不准也不要出现黑块。
    if (!(color.r == color.r) || !(color.g == color.g) || !(color.b == color.b)) {
        color = vec3(0.62, 0.66, 0.70);
    }
    gl_FragColor = vec4(color, alpha);
}
"""

private const val GLOW_VS = """
uniform mat4 uMvp;
attribute vec3 aPos;
attribute float aAlpha;
varying float vAlpha;
void main() {
    vAlpha = aAlpha;
    gl_Position = uMvp * vec4(aPos, 1.0);
}
"""

private const val GLOW_FS = """
precision mediump float;
uniform vec4 uColor;
varying float vAlpha;
void main() {
    gl_FragColor = vec4(uColor.rgb * uColor.a, uColor.a) * vAlpha;
}
"""

/**
 * EGL 配置选择：优先要 4x MSAA，拿不到就退回不带多重采样的配置。
 *
 * 为什么必须带回退：鸿蒙容器的 EGL 实现不一定支持 SAMPLE_BUFFERS=1。
 * 如果直接返回 null，GLSurfaceView 会抛 "No config chosen" 让整个面板挂掉，
 * 所以第二次必须退到普通 8/8/8/0 + 16 位深度（和旧行为完全一致）。
 */
private class MsaaConfigChooser(private val samples: Int) : GLSurfaceView.EGLConfigChooser {
    override fun chooseConfig(egl: EGL10, display: EGLDisplay): EGLConfig? {
        // 深度位宽是车模好不好看的关键：16 位（65536 级）配 near=0.3/far=400，
        // 在车身尺度（相邻面差约 2cm、距相机约 11m）只分辨得出约 1.3 级，
        // 恰好落在 z-fighting 的临界点上。面板拉宽、像素密度翻倍后就会看到碎块。
        // 24 位把精度提到 256 倍，z-fighting 消失。
        fun spec(depth: Int, msaa: Boolean): IntArray {
            val base = intArrayOf(
                EGL10.EGL_RED_SIZE, 8,
                EGL10.EGL_GREEN_SIZE, 8,
                EGL10.EGL_BLUE_SIZE, 8,
                EGL10.EGL_ALPHA_SIZE, 0,
                EGL10.EGL_DEPTH_SIZE, depth,
                EGL10.EGL_STENCIL_SIZE, 0,
                EGL10.EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
            )
            val ms = if (msaa) intArrayOf(EGL10.EGL_SAMPLE_BUFFERS, 1, EGL10.EGL_SAMPLES, samples)
            else intArrayOf()
            return base + ms + intArrayOf(EGL10.EGL_NONE)
        }
        val out = arrayOfNulls<EGLConfig>(1)
        val n = IntArray(1)
        // 必须降级：不支持的设备上如果只试 24 位，chooseConfig 会返回 null，
        // GLSurfaceView 直接抛 "No config chosen"，整块面板挂掉。
        for (cfg in listOf(spec(24, true), spec(24, false), spec(16, false))) {
            if (egl.eglChooseConfig(display, cfg, out, 1, n) && n[0] > 0 && out[0] != null) {
                return out[0]
            }
        }
        return null
    }

    private companion object {
        /** EGL_OPENGL_ES2_BIT，EGL10 里没有这个常量 */
        const val EGL_OPENGL_ES2_BIT = 4
    }
}

private class Program(vs: String, fs: String) {
    val id: Int
    init {
        val v = compile(GLES20.GL_VERTEX_SHADER, vs)
        val f = compile(GLES20.GL_FRAGMENT_SHADER, fs)
        id = GLES20.glCreateProgram().also {
            GLES20.glAttachShader(it, v)
            GLES20.glAttachShader(it, f)
            GLES20.glLinkProgram(it)
            val ok = IntArray(1)
            GLES20.glGetProgramiv(it, GLES20.GL_LINK_STATUS, ok, 0)
            if (ok[0] == 0) {
                val log = GLES20.glGetProgramInfoLog(it)
                GLES20.glDeleteProgram(it)
                throw IllegalStateException("link failed: $log")
            }
        }
        GLES20.glDeleteShader(v)
        GLES20.glDeleteShader(f)
    }
    fun loc(n: String) = GLES20.glGetUniformLocation(id, n)
    fun attr(n: String) = GLES20.glGetAttribLocation(id, n)
    private fun compile(type: Int, src: String): Int {
        val s = GLES20.glCreateShader(type)
        GLES20.glShaderSource(s, src)
        GLES20.glCompileShader(s)
        val ok = IntArray(1)
        GLES20.glGetShaderiv(s, GLES20.GL_COMPILE_STATUS, ok, 0)
        if (ok[0] == 0) {
            val log = GLES20.glGetShaderInfoLog(s)
            GLES20.glDeleteShader(s)
            throw IllegalStateException("compile failed: $log")
        }
        return s
    }
}

/**
 * 环境几何（地面/道路/车道线/树/路灯/阴影/光晕）。
 *
 * 顶点必须走 VBO，不能像以前那样把 client-side FloatBuffer 直接传给
 * glVertexAttribPointer：
 *  · 性能：client 数组每次 draw 都要把顶点从 app 内存拷给驱动；VBO 只传一个 offset。
 *  · 兼容：模拟器（Android Emulator）的 guest GLES 编码器在 glUtilsPackPointerData
 *    里打包 client 指针时会 SIGSEGV——tombstone 里 fault addr 形如 0x402ff00c，
 *    正是被截断成 32 位的 buffer 地址，堆栈是
 *    glVertexAttribPointerData_enc → glUtilsPackPointerData。
 *    真机 Mali/Adreno 直接读 client 数组不会崩，所以这个坑只在模拟器上暴露。
 * 构造必须在 GL 线程（initGl 内），glGenBuffers 需要当前 context。
 */
private class Mesh(vertices: FloatArray) {
    private val vbo = IntArray(1)
    val count = vertices.size / FPV

    init {
        val buf = ByteBuffer
            .allocateDirect(vertices.size * 4)
            .order(ByteOrder.nativeOrder())
            .asFloatBuffer()
            .apply { put(vertices); position(0) }
        GLES20.glGenBuffers(1, vbo, 0)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo[0])
        GLES20.glBufferData(GLES20.GL_ARRAY_BUFFER, vertices.size * 4, buf, GLES20.GL_STATIC_DRAW)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
    }

    fun draw(aPos: Int, aNormal: Int, aMat: Int) {
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo[0])
        GLES20.glVertexAttribPointer(aPos, 3, GLES20.GL_FLOAT, false, STRIDE, 0)
        GLES20.glEnableVertexAttribArray(aPos)
        GLES20.glVertexAttribPointer(aNormal, 3, GLES20.GL_FLOAT, false, STRIDE, 12)
        GLES20.glEnableVertexAttribArray(aNormal)
        GLES20.glVertexAttribPointer(aMat, 2, GLES20.GL_FLOAT, false, STRIDE, 24)
        GLES20.glEnableVertexAttribArray(aMat)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLES, 0, count)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
    }
}

private class GlowMesh(vertices: FloatArray) {
    private val vbo = IntArray(1)
    val count = vertices.size / 4

    init {
        val buf = ByteBuffer
            .allocateDirect(vertices.size * 4)
            .order(ByteOrder.nativeOrder())
            .asFloatBuffer()
            .apply { put(vertices); position(0) }
        GLES20.glGenBuffers(1, vbo, 0)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo[0])
        GLES20.glBufferData(GLES20.GL_ARRAY_BUFFER, vertices.size * 4, buf, GLES20.GL_STATIC_DRAW)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
    }

    fun draw(aPos: Int, aAlpha: Int) {
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo[0])
        GLES20.glVertexAttribPointer(aPos, 3, GLES20.GL_FLOAT, false, 16, 0)
        GLES20.glEnableVertexAttribArray(aPos)
        GLES20.glVertexAttribPointer(aAlpha, 1, GLES20.GL_FLOAT, false, 16, 12)
        GLES20.glEnableVertexAttribArray(aAlpha)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLES, 0, count)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
    }
}

/** 材质参数：高光强度 + 光泽度（0..1，着色器里 ×64 得到指数）。 */
private data class Mat(val spec: Float, val gloss: Float)

private val MAT_ROAD = Mat(0.02f, 0.15f)
private val MAT_PAINT = Mat(0.55f, 0.72f)
private val MAT_GLASS = Mat(0.85f, 0.92f)
private val MAT_TIRE = Mat(0.05f, 0.2f)
private val MAT_LEAF = Mat(0.05f, 0.1f)
private val MAT_LAMP = Mat(0.10f, 0.3f)
private val MAT_LIGHT = Mat(0.9f, 0.95f)

/** 相机在世界坐标中的位置，视图矩阵与光照必须使用同一个值。 */
internal fun sceneCameraEye(gear: String?, speedKph: Float, out: FloatArray) {
    val camY = 4f + speedKph * 0.004f
    val camZ = -8.5f - speedKph * 0.012f
    when (gear) {
        "park" -> { out[0] = -8.6f; out[1] = 3.9f; out[2] = -6.2f }
        "reverse" -> { out[0] = 0f; out[1] = 2.8f; out[2] = -6f }
        else -> { out[0] = -0.8f; out[1] = camY; out[2] = camZ }
    }
}

internal fun sceneCameraTarget(gear: String?, out: FloatArray) {
    out[0] = 0f
    when (gear) {
        // 从车尾后方低机位朝车头看；瞄点保持在车内，避免车尾被卡片底边裁掉。
        "reverse" -> { out[1] = 0.65f; out[2] = 4.5f }
        "park" -> { out[1] = 0.6f; out[2] = 2f }
        else -> { out[1] = 0.5f; out[2] = 11.5f }
    }
}

/** User-selected presentation lighting, not a reported vehicle headlight switch. */
internal fun isDisplayHeadlamp(authoredMaterial: Boolean, lampRole: Int): Boolean =
    authoredMaterial && lampRole == 8

/**
 * Real headlamp lit state. Telemetry wins: when the vehicle reports a low/high beam
 * switch we follow it; only before any lamp telemetry arrives do we keep the
 * presentation headlights lit as the user-requested default.
 */
internal fun headlampOn(headlight: Boolean?, highBeam: Boolean?): Boolean =
    headlight != false || highBeam != false

/** Twin road light pools in vehicle-local coordinates; +Z is the front of this model. */
internal fun headlampBeamVertices(): FloatArray {
    val vertices = ArrayList<Float>()
    val rows = 24
    val columns = 12
    fun vertex(side: Int, row: Int, column: Int) {
        val t = row.toFloat() / rows
        val across = column.toFloat() / columns * 2f - 1f
        val halfWidth = 0.18f + 2.4f * t
        val center = 0.72f + 0.55f * t
        val edge = (1f - across * across).coerceAtLeast(0f)
        val fade = (1f - t) * (1f - t)
        val alpha = (t * 14f).coerceAtMost(1f) * fade * edge * edge
        vertices.add(side * (center + across * halfWidth))
        vertices.add(0.035f) // Above the road/lane surfaces; still depth-occluded by vehicles.
        vertices.add(2.2f + 14f * t)
        vertices.add(alpha)
    }
    for (side in intArrayOf(-1, 1)) for (row in 0 until rows) for (col in 0 until columns) {
        vertex(side, row, col); vertex(side, row + 1, col); vertex(side, row + 1, col + 1)
        vertex(side, row, col); vertex(side, row + 1, col + 1); vertex(side, row, col + 1)
    }
    return vertices.toFloatArray()
}

internal class SceneRenderer(
    private val data: SceneData,
    private val appContext: android.content.Context,
) : GLSurfaceView.Renderer {

    /** 车模的 GPU 资源，GL 线程懒建；数据源换了才重建 */
    private var car: List<CarPart> = emptyList()
    private var carSource: List<CarData>? = null
    private val carTextures = CarTextureCache()
    private var carHasBrakeLamp = false

    // ── 周围车辆（radarState 驱动）────────────────────────────────
    // 用简化盒体表示，不加载第二份 car.bin：他车在画面里只有几十像素高，
    // 精简几何足够，还能把 draw call 控制在每车 3 次。
    private var leadBody: Mesh? = null
    private var leadCabin: Mesh? = null
    private var leadWheels: Mesh? = null
    /** 他车尾灯：刹车时点亮成亮红 */
    private var leadTail: Mesh? = null
    /** 他车下方的地面距离标记：颜色随 dRel 由暖到冷 */
    private var leadMark: Mesh? = null
    private val leadModel = FloatArray(16)
    private val leadOffset = FloatArray(16)

    /** 把 IO 线程解析好的数据搬到显存。只在 GL 线程调用。 */
    private fun syncCar() {
        val src = data.carData
        if (src.isEmpty() || src === carSource) return
        try {
            car.forEach { it.release() }
            carTextures.release()
            car = src.map { CarPart(it, carTextures) }
            carHasBrakeLamp = src.any { it.lampRole == 3 }
            carSource = src
            CrashLog.note("car uploaded: ${car.size} parts, " +
                "${car.sumOf { it.triangleCount }} tris")
        } catch (t: Throwable) {
            // 车模出问题不该拖垮整个面板：退化成程序化车身继续画
            CrashLog.record("syncCar", t)
            car = emptyList()
            carSource = src
        }
    }

    private val snap = SceneData.Snapshot()
    private val cameraEye = FloatArray(3)
    private val cameraTarget = FloatArray(3)
    private var sceneProg: Program? = null
    private var glowProg: Program? = null

    // 几何
    private var ground: Mesh? = null
    private var roadSurface: Mesh? = null
    private var laneLines: Mesh? = null      // 真实车道线（模型数据，静止）
    private var laneSolid: Mesh? = null      // 默认左右实线（静止，连续无接缝）
    private var laneDashes: Mesh? = null     // 默认中间虚线（随里程滚动）
    private var roadSeams: Mesh? = null      // 路面横向接缝（随里程滚动）
    private var carLower: Mesh? = null
    private var carBody: Mesh? = null
    private var carRoof: Mesh? = null
    private var carGlass: Mesh? = null
    private var carWindows: Mesh? = null
    private var carHeadLight: Mesh? = null
    private var carTailLight: Mesh? = null
    private var carWheels: Mesh? = null
    private var trees: Mesh? = null       // 树冠（绿）
    private var trunks: Mesh? = null      // 树干（棕）
    private var lamps: Mesh? = null
    private var shadowQuad: GlowMesh? = null
    private var haloRing: GlowMesh? = null
    private var haloFill: GlowMesh? = null
    private var roadGlow: GlowMesh? = null
    private var headlampBeams: GlowMesh? = null
    private var laneRevision = -1

    private val proj = FloatArray(16)
    private val view = FloatArray(16)
    private val vp = FloatArray(16)
    private val mvp = FloatArray(16)

    // ── 可动件 / 灯 ──
    private val nrm3 = FloatArray(9)          // 法线矩阵（3x3，从 model 左上角取）
    private val partModel = FloatArray(16)
    /** 车模绘制顺序缓存（复用，避免每帧分配） */
    private val carOrder = ArrayList<CarPart>(96)
    private val hingeM = FloatArray(16)
    private val rigAmt = FloatArray(7)        // 各可动件开度 0..1，索引 = kind
    private val lamp3 = FloatArray(3)
    private var lastFrameNs = 0L
    private var dtSec = 0.016f
    private var blinkPhaseOn = false
    // 里程（米）与各层滚动偏移
    private var travelZ = 0f
    private val treeModel = FloatArray(16)
    private val lampModel = FloatArray(16)
    private val dashModel = FloatArray(16)
    private val seamModel = FloatArray(16)
    private var width = 1
    private var height = 1

    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
        try {
            initGl()
        } catch (t: Throwable) {
            CrashLog.record("onSurfaceCreated", t)
            throw t
        }
    }

    private fun initGl() {
        // onSurfaceCreated also runs after EGL context loss: old numeric GL names are invalid.
        car = emptyList()
        carSource = null
        carHasBrakeLamp = false
        carTextures.forgetContext()
        GLES20.glClearColor(BG_R, BG_G, BG_B, 1f)
        GLES20.glEnable(GLES20.GL_DEPTH_TEST)
        GLES20.glDepthFunc(GLES20.GL_LEQUAL)

        sceneProg = Program(SCENE_VS, SCENE_FS)
        glowProg = Program(GLOW_VS, GLOW_FS)

        ground = quad(
            -260f, 0f, -260f, 260f, 0f, -260f, 260f, 0f, 260f, -260f, 0f, 260f,
            0f, 1f, 0f, MAT_ROAD,
        )
        roadSurface = quad(
            -5.2f, 0.01f, -30f, 5.2f, 0.01f, -30f, 5.2f, 0.01f, 240f, -5.2f, 0.01f, 240f,
            0f, 1f, 0f, Mat(0.05f, 0.25f),
        )
        val defaultLanes = buildDefaultLanes()
        laneSolid = defaultLanes.first
        laneDashes = defaultLanes.second
        roadSeams = buildRoadSeams()
        val treePair = buildTrees()
        trees = treePair.first
        trunks = treePair.second
        lamps = buildLamps()
        shadowQuad = buildShadow()
        haloRing = buildRing(1.95f, 2.45f, 0.90f, 0.05f)
        haloFill = buildRing(0f, 1.85f, 0.42f, 0f)
        roadGlow = buildRoadGlow()
        headlampBeams = GlowMesh(headlampBeamVertices())
        // 程序化车身：car.bin 还在解析 / 解析失败 / 与 GL 线程竞争时就画它，
        // 保证面板里始终有一台车而不是空场景。
        // 之前这里漏了调用，导致下面 else 分支拿到的 carLower/carBody/carGlass/carWheels
        // 全是 null（detekt 的 UnusedPrivateMember 把这个漏网之鱼揪出来的）。
        buildCar()
        buildLeadCar()
    }

    private fun buildCar() {
        // 底盘（深灰，略窄）
        carLower = box(-0.88f, 0.26f, -2.26f, 0.88f, 0.56f, 2.26f, Mat(0.18f, 0.4f))
        // 车身（主色，最宽）
        carBody = box(-0.94f, 0.50f, -2.30f, 0.94f, 1.00f, 2.30f, MAT_PAINT)
        // 车顶
        carRoof = box(-0.80f, 1.00f, -1.12f, 0.80f, 1.44f, 0.78f, MAT_PAINT)
        // 玻璃（略大于车顶，露一圈）
        carGlass = box(-0.83f, 1.06f, -1.08f, 0.83f, 1.38f, 0.74f, MAT_GLASS)
        // 侧窗：两侧薄片
        carWindows = box(-0.955f, 1.06f, -0.95f, 0.955f, 1.34f, 0.60f, MAT_GLASS)
        // 前灯（白）
        carHeadLight = box(-0.78f, 0.62f, -2.34f, 0.78f, 0.80f, -2.28f, MAT_LIGHT)
        // 尾灯（红）
        carTailLight = box(-0.80f, 0.66f, 2.28f, 0.80f, 0.84f, 2.34f, Mat(0.5f, 0.6f))
        carWheels = buildWheels()
    }

    /**
     * 他车的简化几何：车身 + 车厢 + 四个轮子。
     * 尺寸按紧凑型轿车（1.8m 宽 × 4.3m 长），比自车小一圈，视觉上容易被区分开。
     */
    private fun buildLeadCar() {
        leadBody = box(-0.90f, 0.26f, -2.15f, 0.90f, 0.95f, 2.15f, MAT_PAINT)
        leadCabin = box(-0.76f, 0.95f, -1.05f, 0.76f, 1.44f, 0.80f, MAT_GLASS)
        // 四个轮子：和自车同款圆柱，半径缩到 0.30
        val v = ArrayList<Float>()
        val r = 0.30f
        val halfW = 0.11f
        for ((cx, cz) in listOf(
            -0.86f to -1.34f, 0.86f to -1.34f,
            -0.86f to 1.38f, 0.86f to 1.38f,
        )) {
            val sides = 8
            for (side in 0 until sides) {
                val a0 = 2.0 * Math.PI * side / sides
                val a1 = 2.0 * Math.PI * (side + 1) / sides
                val y0 = (r * sin(a0)).toFloat(); val z0 = (r * cos(a0)).toFloat()
                val y1 = (r * sin(a1)).toFloat(); val z1 = (r * cos(a1)).toFloat()
                val nx0 = sin(a0).toFloat(); val nz0 = cos(a0).toFloat()
                val nx1 = sin(a1).toFloat(); val nz1 = cos(a1).toFloat()
                addN(v, cx - halfW, y0, cz + z0, nx0, 0f, nz0, MAT_TIRE)
                addN(v, cx + halfW, y0, cz + z0, nx0, 0f, nz0, MAT_TIRE)
                addN(v, cx + halfW, y1, cz + z1, nx1, 0f, nz1, MAT_TIRE)
                addN(v, cx - halfW, y0, cz + z0, nx0, 0f, nz0, MAT_TIRE)
                addN(v, cx + halfW, y1, cz + z1, nx1, 0f, nz1, MAT_TIRE)
                addN(v, cx - halfW, y1, cz + z1, nx1, 0f, nz1, MAT_TIRE)
            }
        }
        leadWheels = Mesh(v.toFloatArray())
        // 尾灯：贴在他车尾端（-z 侧，朝自车那一面）。
        // 比车身外凸 1cm，避免与车身共面导致 z-fighting（8.9.35 那批问题的老套路）。
        leadTail = box(-0.80f, 0.62f, -2.19f, 0.80f, 0.82f, -2.13f, MAT_LIGHT)
        // 车下距离标记：贴地的薄片，颜色在 drawLeads 里按 dRel 插值。
        // 抬到 y=1.8~3.0cm 高于路面与车道线，避免同为 y≈0 时 z-fighting。
        leadMark = box(-1.35f, 0.018f, -2.90f, 1.35f, 0.030f, 2.90f, MAT_PAINT)
    }

    /**
     * 画周围车辆。位置来自 radarState：
     *   z = forwardM（前方为正，和场景一致）
     *   x = **+lateralM**
     *
     * 符号容易搞错，这里记清楚：openpilot 的 yRel 是"左正"（x 前 / y 左 / z 上）。
     * 场景 WORLD 坐标是 x 右正、z 前正。但相机站在 z 负侧朝 **+z** 看，
     * 右手系下 setLookAtM 算出来的"屏幕右"对应世界 **−x**。
     * 两个负号抵消，所以世界 x 要直接取 +yRel：
     *   车在左车道（yRel=+3.5）→ world x=+3.5 → 屏幕左 ✓
     *   车在右车道（yRel=−3.5）→ world x=−3.5 → 屏幕右 ✓
     * 写成 −yRel 会让左右车道整体互换（实测：yRel=+3.5 画到了屏幕右侧）。
     */
    private fun drawLeads(
        sp: Program, carModel: FloatArray, uMvp: Int, uModel: Int, uColor: Int,
        aPos: Int, aNormal: Int, aMat: Int,
    ) {
        for (lead in snap.leads) {
            val fwd = fin(lead.forwardM)
            if (fwd <= 1f || fwd > 150f) continue
            val lat = fin(lead.lateralM)
            if (lat.isNaN() || kotlin.math.abs(lat) > 8f) continue
            Matrix.setIdentityM(leadOffset, 0)
            Matrix.translateM(leadOffset, 0, lat, 0f, fwd)
            Matrix.multiplyMM(leadModel, 0, carModel, 0, leadOffset, 0)
            // 车下距离标记：8m 内最暖（橙褐），50m 外最冷（青灰），中间线性插值。
            // 这样一眼能看出"哪辆离我近"，比单纯靠透视大小可靠得多 ——
            // 尤其远车在画面上只差几个像素时。
            val gap = ((fwd - 8f) / 42f).coerceIn(0f, 1f)
            drawMesh(sp, leadMark, uMvp, uModel, uColor, aPos, aNormal, aMat,
                0.72f + (0.36f - 0.72f) * gap,
                0.36f + (0.46f - 0.36f) * gap,
                0.22f + (0.60f - 0.22f) * gap,
                leadModel)
            drawMesh(sp, leadBody, uMvp, uModel, uColor, aPos, aNormal, aMat,
                0.86f, 0.88f, 0.90f, leadModel)
            drawMesh(sp, leadCabin, uMvp, uModel, uColor, aPos, aNormal, aMat,
                0.22f, 0.26f, 0.33f, leadModel)
            drawMesh(sp, leadWheels, uMvp, uModel, uColor, aPos, aNormal, aMat,
                0.09f, 0.09f, 0.10f, leadModel)
            // 尾灯：与自车同向行驶，所以我们看到的是他车车尾（-z 端）。
            // 刹车时整条点亮成亮红，松开回落成暗红 —— 真车尾灯本来就是常亮的。
            if (lead.braking) {
                drawMesh(sp, leadTail, uMvp, uModel, uColor, aPos, aNormal, aMat,
                    1.00f, 0.10f, 0.08f, leadModel)
            } else {
                drawMesh(sp, leadTail, uMvp, uModel, uColor, aPos, aNormal, aMat,
                    0.34f, 0.06f, 0.05f, leadModel)
            }
        }
    }

    override fun onSurfaceChanged(gl: GL10?, w: Int, h: Int) {
        width = max(w, 1)
        height = max(h, 1)
        GLES20.glViewport(0, 0, width, height)
        // near/far 决定深度精度（比值越小越准）。相机 y=4.0 俯视 9.9°：
        // 地面最近点 23m 外、车尾 8.75m 外，near=2.0 不会裁掉任何东西；
        // 树最远排到 250m，far=280 留出余量。
        // 400/1.0 = 400 → 280/2.0 = 140，精度提升约 2.9 倍。
        // 起因：车尾下护板朝下，相机从后上方看接近掠射，2~5mm 的真实间隙沿视线
        // 只剩约 0.44mm（≈7 个深度单位），与三角形深度插值误差（≈1.7 单位）同量级，
        // 于是逐像素翻转成斑马纹。实测 speed=0 高频能量 8.06、speed=80（俯角大）只有 1.09。
        Matrix.perspectiveM(proj, 0, 46f, width.toFloat() / height.toFloat(), 2.0f, 280f)
    }

    override fun onDrawFrame(gl: GL10?) {
        try {
            drawFrame()
        } catch (t: Throwable) {
            // 只记第一次，避免 60fps 刷屏把存储写满
            if (crashLogged) throw t
            crashLogged = true
            CrashLog.record("onDrawFrame", t)
            throw t
        }
    }

    private var crashLogged = false

    private fun drawFrame() {
        val nowNs = System.nanoTime()
        blinkPhaseOn = (nowNs / 200_000_000L) % 2L == 0L
        dtSec = if (lastFrameNs == 0L) 0.016f
            else ((nowNs - lastFrameNs) / 1_000_000_000.0).toFloat().coerceIn(0.001f, 0.1f)
        lastFrameNs = nowNs
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT or GLES20.GL_DEPTH_BUFFER_BIT)
        val sp = sceneProg ?: return
        syncCar()
        data.read(snap)
        if (laneRevision != snap.revision) {
            if (snap.curves.isNotEmpty()) laneLines = buildRealLanes(snap.curves)
            laneRevision = snap.revision
        }

        // 所有进入矩阵的浮点先净化：coerceIn 对 NaN 是透传的（NaN 与任何数比较都是
        // false，于是原样返回），车端一旦送来 NaN（vEgo 在某些状态就是 NaN），
        // rollDeg 会变 NaN，Matrix.rotateM 整行失效，车模直接消失或整块变黑。
        val speed = fin(snap.speedKph).coerceIn(0f, 220f)
        val steer = fin(snap.steeringDeg).coerceIn(-720f, 720f)
        val rollDeg = (-steer * 0.05f * (speed / 60f).coerceIn(0f, 1f)).coerceIn(-4f, 4f)
        val pitchDeg = if (snap.braking) 1.3f else -0.3f * (speed / 100f).coerceIn(0f, 1f)

        // 相机：第三人称追尾视角（车机 SR 那种"司机后脑勺斜上方"的机位）。
        //
        // 之前 camY=6.1 / 看向 y=0.55，光轴俯角 arctan(5.55/10.5) ≈ 28°，等于从二楼
        // 往下看车，车顶天窗占满、车尾正脸被压缩，像在展示模型而不是在路上开。
        // 现在按目标构图反推：地平线落在画面上方约 1/3、自车落在 58%~75%，
        // 对应光轴俯角约 8°。垂直 FOV 是 46°（半角 23°）：
        //   地平线在中心上方 8/23 = 35% 半高处；
        //   车顶俯角 11.8°、车尾俯角 19.5°，都大于 8° → 整车在中心下方，占 58%~75%。
        // 俯角再小车载画面会往上跑，再大车顶就跑进画面中心以上（又变回俯视感）。
        sceneCameraEye(snap.gear, speed, cameraEye)
        sceneCameraTarget(snap.gear, cameraTarget)
        val eyeX = cameraEye[0]; val eyeY = cameraEye[1]; val eyeZ = cameraEye[2]
        Matrix.setLookAtM(view, 0, eyeX, eyeY, eyeZ,
            cameraTarget[0], cameraTarget[1], cameraTarget[2], 0f, 1f, 0f)
        Matrix.multiplyMM(vp, 0, proj, 0, view, 0)

        GLES20.glUseProgram(sp.id)
        val uMvp = sp.loc("uMvp")
        val uModel = sp.loc("uModel")
        val uColor = sp.loc("uColor")
        val uFog = sp.loc("uFogColor")
        val uSky = sp.loc("uSkyColor")
        val uGround = sp.loc("uGroundColor")
        val uLight = sp.loc("uLightDir")
        val uView = sp.loc("uViewPos")
        val uAmb = sp.loc("uAmbient")
        val uDif = sp.loc("uDiffuse")
        val aPos = sp.attr("aPos")
        val aNormal = sp.attr("aNormal")
        val aMat = sp.attr("aMat")
        val uNormalMat = sp.loc("uNormalMat")
        val uLampColor = sp.loc("uLamp")


        GLES20.glUniform3f(uFog, BG_R, BG_G, BG_B)
        GLES20.glUniform3f(uSky, SKY_R, SKY_G, SKY_B)
        GLES20.glUniform3f(uGround, 0.30f, 0.30f, 0.28f)   // 白天地面反光偏暖灰
        GLES20.glUniform3f(uLight, 0.45f, 0.88f, -0.20f)   // 白天：太阳在右上前方
        GLES20.glUniform3f(uView, eyeX, eyeY, eyeZ)
        GLES20.glUniform1f(uAmb, 0.88f)    // 白天环境光很强，阴影里也看得清
        GLES20.glUniform1f(uDif, 0.78f)

        val ident = FloatArray(16).also { Matrix.setIdentityM(it, 0) }

        drawMesh(sp, ground, uMvp, uModel, uColor, aPos, aNormal, aMat,
            ROAD_R, ROAD_G, ROAD_B, ident)
        drawMesh(sp, roadSurface, uMvp, uModel, uColor, aPos, aNormal, aMat,
            0.46f, 0.47f, 0.48f, ident)

        // 环境滚动：各自按周期取模后沿 -z 平移。周期都等于几何的重复间距，
        // 所以平移一个周期后与平移前完全重合，视觉连续。
        scrollMatrix(treeModel, travelZ % TREE_PERIOD)
        scrollMatrix(lampModel, travelZ % LAMP_PERIOD)
        scrollMatrix(dashModel, travelZ % DASH_PERIOD)
        scrollMatrix(seamModel, travelZ % SEAM_PERIOD)

        drawMesh(sp, laneSolid, uMvp, uModel, uColor, aPos, aNormal, aMat,
            0.92f, 0.92f, 0.88f, ident)
        drawMesh(sp, laneDashes, uMvp, uModel, uColor, aPos, aNormal, aMat,
            0.97f, 0.97f, 0.94f, dashModel)
        drawMesh(sp, roadSeams, uMvp, uModel, uColor, aPos, aNormal, aMat,
            0.40f, 0.41f, 0.42f, seamModel)
        // 真实车道线是模型数据，跟着车走，不随里程滚动
        if (laneLines != null) {
            drawMesh(sp, laneLines, uMvp, uModel, uColor, aPos, aNormal, aMat,
                0.97f, 0.97f, 0.94f, ident)
        }
        drawMesh(sp, trees, uMvp, uModel, uColor, aPos, aNormal, aMat,
            0.26f, 0.52f, 0.24f, treeModel)
        drawMesh(sp, trunks, uMvp, uModel, uColor, aPos, aNormal, aMat,
            0.34f, 0.26f, 0.18f, treeModel)
        drawMesh(sp, lamps, uMvp, uModel, uColor, aPos, aNormal, aMat,
            0.62f, 0.64f, 0.66f, lampModel)

        // 自车（整体 + 姿态）
        val carModel = FloatArray(16)
        Matrix.setIdentityM(carModel, 0)
        Matrix.translateM(carModel, 0, 0f, 0f, 2.6f)
        Matrix.rotateM(carModel, 0, rollDeg, 0f, 0f, 1f)
        Matrix.rotateM(carModel, 0, pitchDeg, 1f, 0f, 0f)

        // 周围车辆（radarState 驱动）。放在自车 pass 之前画：
        // 他车都在前方，深度测试自然处理遮挡；简化几何只有 3 个 mesh，不需要分层。
        // 父变换用 carModel，所以 forwardM 是"相对自车"的距离，正好对上 radar 的语义。
        drawLeads(sp, carModel, uMvp, uModel, uColor, aPos, aNormal, aMat)

        if (car.isNotEmpty()) {
            val uAlpha = sp.loc("uAlpha")
            GLES20.glUniform1f(uAlpha, 1f)
            // 每帧推进门的开合缓动
            stepRig(dtSec)
            // 共面材质排序（见 depthLayer）：深度相等时靠 GL_LESS 让先画的赢
            GLES20.glDepthFunc(GLES20.GL_LESS)
            // 配合下面的按材质深度偏移（只对镀铬塑料生效）
            GLES20.glEnable(GLES20.GL_POLYGON_OFFSET_FILL)
            // 第一遍：不透明件。按材质层级升序画（最外的先画 → GL_LESS 下它赢），
            // 这样模型里那些共面的重复几何（前门窗框、门内饰、车尾下护板）不会再逐像素闪烁。
            for (part in car) {
                if (part.isTransparent) continue
                carOrder.add(part)
            }
            carOrder.sortBy { depthLayer(it.name) }
            for (part in carOrder) {
                drawCarPart(part, carModel, uMvp, uModel, uColor, uNormalMat, uLampColor, aPos, aNormal, aMat, uAlpha)
            }
            carOrder.clear()
            // 第二遍：玻璃单独一遍 —— 开混合、不写深度，才能透出座椅和内饰，
            // 否则玻璃会按绘制顺序把后面的东西全遮掉。
            val glasses = car.filter { it.isTransparent }.sortedByDescending { part ->
                makePartModel(part, carModel)
                val c = part.center
                val x = partModel[0] * c[0] + partModel[4] * c[1] + partModel[8] * c[2] + partModel[12] - eyeX
                val y = partModel[1] * c[0] + partModel[5] * c[1] + partModel[9] * c[2] + partModel[13] - eyeY
                val z = partModel[2] * c[0] + partModel[6] * c[1] + partModel[10] * c[2] + partModel[14] - eyeZ
                x * x + y * y + z * z
            }
            if (glasses.isNotEmpty()) {
                GLES20.glEnable(GLES20.GL_BLEND)
                GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA)
                GLES20.glDepthMask(false)
                for (part in glasses) {
                    drawCarPart(part, carModel, uMvp, uModel, uColor, uNormalMat, uLampColor, aPos, aNormal, aMat, uAlpha)
                }
                GLES20.glDepthMask(true)
                GLES20.glDisable(GLES20.GL_BLEND)
            }
            val lampOverlays = car.filter { part ->
                isTurnLampOverlayActive(part.hasAuthoredMaterial, part.lampRole,
                    snap.leftBlinker, snap.rightBlinker, blinkPhaseOn) ||
                    (isDisplayHeadlamp(part.hasAuthoredMaterial, part.lampRole) &&
                        headlampOn(snap.headlight, snap.highBeam))
            }
            if (lampOverlays.isNotEmpty()) {
                drawTurnLampOverlayPass(sp.loc("uLampOverlay"), sp.loc("uNormalOffset")) {
                    for (part in lampOverlays) {
                        drawCarPart(part, carModel, uMvp, uModel, uColor, uNormalMat, uLampColor,
                            aPos, aNormal, aMat, uAlpha, lampOverlay = true)
                    }
                }
            }
            GLES20.glDepthFunc(GLES20.GL_LEQUAL)
            GLES20.glPolygonOffset(0f, 0f)
            GLES20.glDisable(GLES20.GL_POLYGON_OFFSET_FILL)
        } else {
            // 车模没加载上时退回程序化车身，至少还有一台车
            drawMesh(sp, carLower, uMvp, uModel, uColor, aPos, aNormal, aMat,
                0.10f, 0.11f, 0.13f, carModel)
            drawMesh(sp, carBody, uMvp, uModel, uColor, aPos, aNormal, aMat,
                0.90f, 0.93f, 0.96f, carModel)
            drawMesh(sp, carGlass, uMvp, uModel, uColor, aPos, aNormal, aMat,
                0.10f, 0.16f, 0.26f, carModel)
            drawMesh(sp, carWheels, uMvp, uModel, uColor, aPos, aNormal, aMat,
                0.045f, 0.045f, 0.05f, carModel)
        }

        drawGlowLayer(carModel, rollDeg)
    }

    /**
     * 门/盖开合缓动：目标由 SceneData 的门/尾门状态给，这里做指数逼近，
     * 避免状态一跳就"啪"地弹开。rigKind 1..4 分别对应左前/右前/左后/右后门，
     * 独立读取实车四门状态；rigKind 6 为尾门。
     */
    private fun stepRig(dt: Float) {
        // 里程推进：车速 m/s × dt。环境各层按自己的周期取模平移，
        // 所以这里不需要做任何回绕，累加即可。
        // dt 在首帧是 (nanoTime-0)/1e9 这种巨大的值，speed 也可能是 NaN，
        // 一旦让 travelZ 变成 NaN，之后每一帧的滚动偏移都是 NaN，树和车道线永久消失。
        val dtSafe = fin(dt).coerceIn(0f, 0.25f)
        travelZ += (fin(snap.speedKph).coerceIn(0f, 220f) / 3.6f) * dtSafe
        if (!travelZ.isFinite()) travelZ = 0f
        if (travelZ > 1e6f) travelZ -= 1e6f
        val k = kotlin.math.min(1f, dt * 7f)
        val doorTargets = floatArrayOf(
            if (snap.doorFrontLeft == true) 1f else 0f,
            if (snap.doorFrontRight == true) 1f else 0f,
            if (snap.doorRearLeft == true) 1f else 0f,
            if (snap.doorRearRight == true) 1f else 0f,
        )
        val trunkTarget = if (snap.tailgateOpen == true) 1f else 0f
        for (kind in 1..4) rigAmt[kind] += (doorTargets[kind - 1] - rigAmt[kind]) * k
        rigAmt[5] += (0f - rigAmt[5]) * k          // 引擎盖：无数据源，保持关闭
        rigAmt[6] += (trunkTarget - rigAmt[6]) * k
    }

    /** 按灯角色算自发光色。转向灯是 2.5Hz 方波（和车卡默认闪法一致）。 */
    private fun lampColor(role: Int, out: FloatArray, authoredMaterial: Boolean = false) {
        out[0] = 0f; out[1] = 0f; out[2] = 0f
        if (role == 0) return
        when (role) {
            1, 2 -> if ((if (role == 1) snap.leftBlinker else snap.rightBlinker) == true && blinkPhaseOn) {
                out[0] = 1.00f; out[1] = 0.30f; out[2] = 0.02f
            }
            3 -> if (snap.brakeLights == true) { out[0] = 0.95f; out[1] = 0.06f; out[2] = 0.04f }
            4 -> if (snap.gear == "reverse") { out[0] = 0.90f; out[1] = 0.93f; out[2] = 0.98f }
            5 -> { out[0] = 0.78f; out[1] = 0.80f; out[2] = 0.84f }   // 日行灯常亮
            6 -> if (authoredMaterial) {
                out[0] = 0.82f; out[1] = 0.88f; out[2] = 1f  // 宋 PLUS 配置的浅蓝白示宽灯
            } else { out[0] = 0.84f; out[1] = 0.28f; out[2] = 0.16f }
            7 -> { out[0] = 0.70f; out[1] = 0.09f; out[2] = 0.07f }   // 尾灯常亮
            8 -> if (isDisplayHeadlamp(authoredMaterial, role) && headlampOn(snap.headlight, snap.highBeam)) {
                out[0] = 1f; out[1] = 0.98f; out[2] = 0.92f // 真实近/远光优先，无遥测时按默认点亮。
            }
        }
    }

    /** 画一个车模部件：可动件绕自己的铰链转，再乘车身矩阵。 */
    private fun drawCarPart(
        part: CarPart, carModel: FloatArray,
        uMvp: Int, uModel: Int, uColor: Int, uNormalMat: Int, uLampColor: Int,
        aPos: Int, aNormal: Int, aMat: Int, uAlpha: Int,
        lampOverlay: Boolean = false,
    ) {
        makePartModel(part, carModel)
        Matrix.multiplyMM(mvp, 0, vp, 0, partModel, 0)
        GLES20.glUniformMatrix4fv(uMvp, 1, false, mvp, 0)
        GLES20.glUniformMatrix4fv(uModel, 1, false, partModel, 0)
        matrix3Of(partModel, nrm3)
        GLES20.glUniformMatrix3fv(uNormalMat, 1, false, nrm3, 0)
        val c = if (!part.isGlass && !part.isLamp) (debugMatColor(part.name) ?: part.color)
        else part.color
        GLES20.glUniform3f(uColor, c[0], c[1], c[2])
        lampColor(part.lampRole, lamp3, part.hasAuthoredMaterial)
        // The source model uses a separate lit surface; its emission is not dimmed by lens alpha=0.25.
        if (!lampOverlay && (usesTurnLampOverlay(part.hasAuthoredMaterial, part.lampRole) ||
                isDisplayHeadlamp(part.hasAuthoredMaterial, part.lampRole))) lamp3.fill(0f)
        val lampIntensity = tailLampIntensity(part.hasAuthoredMaterial, part.lampRole,
            carHasBrakeLamp, snap.brakeLights)
        for (channel in 0..2) lamp3[channel] *= lampIntensity
        GLES20.glUniform3f(uLampColor, lamp3[0], lamp3[1], lamp3[2])
        GLES20.glUniform1f(uAlpha, part.alpha)
        // Only the legacy model needs the observed ChromePlastic depth workaround.
        val outPlastic = !part.hasAuthoredMaterial && !part.isGlass && !part.isLamp &&
            !part.name.contains("Internal") && part.name.contains("ChromePlastic")
        GLES20.glPolygonOffset(if (outPlastic) -1f else 0f,
            if (lampOverlay) -2f else if (outPlastic) -100f else 0f)
        val sp = sceneProg ?: return
        GLES20.glUniform1f(sp.loc("uLampOverlay"), if (lampOverlay) 1f else 0f)
        GLES20.glUniform1f(sp.loc("uNormalOffset"), if (lampOverlay) TURN_LAMP_NORMAL_OFFSET else 0f)
        part.draw(aPos, aNormal, aMat, uAlpha, sp.attr("aUv"), sp.loc("uUseTexture"),
            sp.loc("uTexture"), sp.loc("uAlphaMode"), sp.loc("uAlphaCutoff"))
    }

    private fun makePartModel(part: CarPart, carModel: FloatArray) {
        val kind = part.rigKind
        if (kind == 0) {
            System.arraycopy(carModel, 0, partModel, 0, 16)
        } else {
            val amt = rigAmt[kind]
            val p = part.pivot
            val ang = part.angleDeg * amt
            Matrix.setIdentityM(hingeM, 0)
            Matrix.translateM(hingeM, 0, p[0], p[1], p[2])
            if (part.hingeAxis == 1) Matrix.rotateM(hingeM, 0, ang, 0f, 1f, 0f)
            else Matrix.rotateM(hingeM, 0, ang, 1f, 0f, 0f)
            Matrix.translateM(hingeM, 0, -p[0], -p[1], -p[2])
            Matrix.multiplyMM(partModel, 0, carModel, 0, hingeM, 0)
        }
    }

    /**
     * 材质深度层级（0=最外，4=最内）。
     *
     * 模型里有冗余的共面几何：前门窗框（y1.15~1.30）和门内饰（y0.48~0.75）
     * 存在**顶点位置完全相同（0.00mm）**的异材质双层，实测 doorFL 里
     * 镀铬塑料×车漆 57 对、镀铬塑料×玻璃 128 对、玻璃×车漆 153 对、皮革×M_Int 50 对。
     * 深度值完全相等时 GL_LEQUAL 谁赢取决于三角形提交顺序，逐像素随机，
     * 表现为"深色内饰上冒出白色碎块"。24 位深度救不了——差值是 0，不是精度不够。
     *
     * 这里按材质分层给深度偏移，让该露出的那层稳定赢（镀铬盖住车漆、皮革盖住 M_Int）。
     * 层级关系是从实测重合对反推的：镀铬塑料>亮铬>车漆>玻璃>皮革>织物
     * >内部塑料件>灰色小件>M_Int。这个值只当作**绘制排序键**（配合 GL_LESS），
     * 不再当深度偏移用——polygon offset 试过 1/8 unit 两档，1 太小压不住浮点误差，
     * 8 会把本来正确的排序也推乱、让车身侧面冒出新的碎块，已放弃。
     */
    /** 临时诊断：把材质染成纯色，用来判定斑块由哪些材质构成。交付版必须关掉。 */
    private fun debugMatColor(name: String): FloatArray? {
        if (!DEBUG_MAT) return null
        return when {
            name.contains("Paint") -> floatArrayOf(1f, 1f, 1f)            // 白
            name.contains("ChromePlastic") && name.contains("Internal") ->
                floatArrayOf(1f, 0f, 0f)                                  // 红 = 内部塑料件
            name.contains("ChromePlastic") -> floatArrayOf(0f, 1f, 0f)    // 绿
            name.contains("Chrome") -> floatArrayOf(0f, 0.35f, 1f)        // 蓝
            name == "M_Int" || name.contains("材质") -> floatArrayOf(1f, 1f, 0f)  // 黄
            name.contains("Leather") -> floatArrayOf(1f, 0f, 1f)          // 品红
            name.contains("Cloth") -> floatArrayOf(0f, 1f, 1f)            // 青
            else -> floatArrayOf(0.9f, 0.5f, 0f)                          // 橙 = 其他
        }
    }

    private fun depthLayer(name: String): Float = when {
        // Internal 必须先判：M_ChromePlastic_Internal 名字里也含 "ChromePlastic"，
        // 顺序反了会被当成外层饰条，反而把内饰盖掉。
        name.contains("Internal") -> 6f
        name.contains("ChromePlastic") -> 0f
        name.contains("Chrome") -> 1f
        name.contains("Paint") -> 2f
        name.contains("Glass") || name.contains("Mirror") || name.contains("Window") -> 3f
        name.contains("Leather") -> 4f
        name.contains("Cloth") || name.contains("Fabric") -> 5f
        name == "#bcbcbc" -> 7f
        name == "#707070" -> 8f
        name == "#454545" -> 9f
        else -> 10f                       // M_Int / 「材质」等最内
    }

    /** 浮点净化：NaN / Infinity 一律当 0，避免污染矩阵和累加量。 */
    private fun fin(v: Float): Float = if (v.isFinite()) v else 0f

    /** 环境滚动：只沿 z 负方向平移 offset 米。 */
    private fun scrollMatrix(out: FloatArray, offset: Float) {
        Matrix.setIdentityM(out, 0)
        Matrix.translateM(out, 0, 0f, 0f, -offset)
    }

    /** 取 4x4 左上 3x3（刚体变换：旋转部分就是法线矩阵，无需逆转置）。 */
    private fun matrix3Of(m: FloatArray, out: FloatArray) {
        out[0] = m[0]; out[1] = m[1]; out[2] = m[2]
        out[3] = m[4]; out[4] = m[5]; out[5] = m[6]
        out[6] = m[8]; out[7] = m[9]; out[8] = m[10]
    }

    private fun drawMesh(
        p: Program, mesh: Mesh?, uMvp: Int, uModel: Int, uColor: Int,
        aPos: Int, aNormal: Int, aMat: Int,
        r: Float, g: Float, b: Float, modelMat: FloatArray,
    ) {
        val m = mesh ?: return
        Matrix.multiplyMM(mvp, 0, vp, 0, modelMat, 0)
        GLES20.glUniformMatrix4fv(uMvp, 1, false, mvp, 0)
        GLES20.glUniformMatrix4fv(uModel, 1, false, modelMat, 0)
        // 顶点着色器用 uNormalMat 转法线；场景物体（地面/道路/树/路灯/程序化车身）
        // 也必须喂进去，否则法线为 0，整屏只剩雾色。
        matrix3Of(modelMat, nrm3)
        GLES20.glUniformMatrix3fv(p.loc("uNormalMat"), 1, false, nrm3, 0)
        GLES20.glUniform3f(uColor, r, g, b)
        GLES20.glUniform3f(p.loc("uLamp"), 0f, 0f, 0f)
        GLES20.glUniform1f(p.loc("uLampOverlay"), 0f)
        GLES20.glUniform1f(p.loc("uNormalOffset"), 0f)
        GLES20.glUniform1f(p.loc("uAlpha"), 1f)
        resetCarTextureState(p.attr("aUv"), p.loc("uUseTexture"), p.loc("uAlphaMode"), p.loc("uAlphaCutoff"))
        m.draw(aPos, aNormal, aMat)
    }

    /** 阴影 + 光圈 + 光带，全部加法/普通混合，画在最后且不写深度。 */
    private fun drawGlowLayer(carModel: FloatArray, roll: Float) {
        val gp = glowProg ?: return
        GLES20.glUseProgram(gp.id)
        val uMvp = gp.loc("uMvp")
        val uColor = gp.loc("uColor")
        val aPos = gp.attr("aPos")
        val aAlpha = gp.attr("aAlpha")
        val mMvp = FloatArray(16)

        GLES20.glEnable(GLES20.GL_BLEND)
        GLES20.glDepthMask(false)

        // 接地阴影：普通混合压暗地面（先画，让光圈盖在上面）
        GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA)
        val shadowModel = FloatArray(16)
        Matrix.setIdentityM(shadowModel, 0)
        Matrix.translateM(shadowModel, 0, 0f, 0.004f, 2.6f)
        Matrix.rotateM(shadowModel, 0, roll, 0f, 0f, 1f)
        Matrix.multiplyMM(mMvp, 0, vp, 0, shadowModel, 0)
        GLES20.glUniformMatrix4fv(uMvp, 1, false, mMvp, 0)
        GLES20.glUniform4f(uColor, 0f, 0f, 0f, 0.72f)
        shadowQuad?.draw(aPos, aAlpha)

        // 光圈：加法混合
        GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE)
        val haloModel = FloatArray(16)
        Matrix.setIdentityM(haloModel, 0)
        Matrix.translateM(haloModel, 0, 0f, 0.012f, 2.6f)
        Matrix.rotateM(haloModel, 0, roll, 0f, 0f, 1f)
        Matrix.multiplyMM(mMvp, 0, vp, 0, haloModel, 0)
        GLES20.glUniformMatrix4fv(uMvp, 1, false, mMvp, 0)
        GLES20.glUniform4f(uColor, GLOW_R, GLOW_G, GLOW_B, 0.16f)
        haloFill?.draw(aPos, aAlpha)
        GLES20.glUniform4f(uColor, GLOW_R, GLOW_G, GLOW_B, 0.34f)
        haloRing?.draw(aPos, aAlpha)

        // 车前光带：加宽、变柔
        val glowModel = FloatArray(16)
        Matrix.setIdentityM(glowModel, 0)
        Matrix.translateM(glowModel, 0, 0f, 0.008f, 5.4f)
        Matrix.multiplyMM(mMvp, 0, vp, 0, glowModel, 0)
        GLES20.glUniformMatrix4fv(uMvp, 1, false, mMvp, 0)
        GLES20.glUniform4f(uColor, GLOW_R, GLOW_G, GLOW_B, 0.10f)
        roadGlow?.draw(aPos, aAlpha)

        // Headlight beams: real near/high beam wins, default-presentation fallback before telemetry.
        if (car.any { isDisplayHeadlamp(it.hasAuthoredMaterial, it.lampRole) } &&
            headlampOn(snap.headlight, snap.highBeam)) {
            Matrix.setIdentityM(glowModel, 0)
            Matrix.translateM(glowModel, 0, 0f, 0f, 2.6f)
            Matrix.multiplyMM(mMvp, 0, vp, 0, glowModel, 0)
            GLES20.glUniformMatrix4fv(uMvp, 1, false, mMvp, 0)
            // GLOW_FS already premultiplies RGB by alpha; do not apply alpha a second time.
            GLES20.glBlendFunc(GLES20.GL_ONE, GLES20.GL_ONE)
            GLES20.glUniform4f(uColor, 1f, 0.98f, 0.90f, 0.52f)
            headlampBeams?.draw(aPos, aAlpha)
        }

        GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA)
        GLES20.glDepthMask(true)
        GLES20.glDisable(GLES20.GL_BLEND)
    }

    /**
     * 默认三车道：左右实线（静止）+ 中间虚线（随里程滚动）。
     * 实线是连续的，滚不滚看不出来，没必要拆；虚线必须按 [DASH_PERIOD] 严格重复，
     * 否则滚动一个周期后接不上，会出现"跳一下"。
     */
    private fun buildDefaultLanes(): Pair<Mesh, Mesh> {
        val solid = ArrayList<Float>()
        for (x in floatArrayOf(-3.4f, 3.4f)) {
            addV(solid, x - 0.07f, 0.02f, -60f, MAT_PAINT)
            addV(solid, x + 0.07f, 0.02f, -60f, MAT_PAINT)
            addV(solid, x + 0.07f, 0.02f, 240f, MAT_PAINT)
            addV(solid, x - 0.07f, 0.02f, -60f, MAT_PAINT)
            addV(solid, x + 0.07f, 0.02f, 240f, MAT_PAINT)
            addV(solid, x - 0.07f, 0.02f, 240f, MAT_PAINT)
        }
        val dash = ArrayList<Float>()
        // 两个周期再多画一格，保证滚动到边界时前方仍有虚线
        var k = -3
        while (k * DASH_PERIOD < 240f) {
            val z = k * DASH_PERIOD
            val z1 = z + 4f
            addV(dash, -0.07f, 0.02f, z, MAT_PAINT)
            addV(dash, 0.07f, 0.02f, z, MAT_PAINT)
            addV(dash, 0.07f, 0.02f, z1, MAT_PAINT)
            addV(dash, -0.07f, 0.02f, z, MAT_PAINT)
            addV(dash, 0.07f, 0.02f, z1, MAT_PAINT)
            addV(dash, -0.07f, 0.02f, z1, MAT_PAINT)
            k++
        }
        return Mesh(solid.toFloatArray()) to Mesh(dash.toFloatArray())
    }

    /**
     * 路面横向接缝：横跨路宽的细线，每 [SEAM_PERIOD] 米一道。
     * 车道虚线间隔 9m，近处几乎看不出速度；接缝密、横贯全宽，前进感主要靠它。
     */
    private fun buildRoadSeams(): Mesh {
        val out = ArrayList<Float>()
        var k = -3
        while (k * SEAM_PERIOD < 240f) {
            val z = k * SEAM_PERIOD
            for (zOff in floatArrayOf(0f, 0.10f)) {
                addV(out, -5.1f, 0.012f, z + zOff, MAT_PAINT)
                addV(out, 5.1f, 0.012f, z + zOff, MAT_PAINT)
                addV(out, 5.1f, 0.012f, z + zOff + 0.10f, MAT_PAINT)
                addV(out, -5.1f, 0.012f, z + zOff, MAT_PAINT)
                addV(out, 5.1f, 0.012f, z + zOff + 0.10f, MAT_PAINT)
                addV(out, -5.1f, 0.012f, z + zOff + 0.10f, MAT_PAINT)
            }
            k++
        }
        return Mesh(out.toFloatArray())
    }

    private fun buildRealLanes(curves: List<FloatArray>): Mesh? {
        val out = ArrayList<Float>(curves.size * 320)
        for (curve in curves) {
            if (curve.size < 6) continue
            var i = 0
            while (i + 6 <= curve.size) {
                val ax = curve[i]; val az = curve[i + 2]
                val bx = curve[i + 3]; val bz = curve[i + 5]
                val dx = bx - ax
                val dz = bz - az
                val len = kotlin.math.sqrt(dx * dx + dz * dz)
                if (len < 0.01f) { i += 3; continue }
                val nx = -dz / len
                val nz = dx / len
                val wA = if (az > 30f) 0.06f else 0.13f
                val wB = if (bz > 30f) 0.06f else 0.13f
                addV(out, ax + nx * wA, 0.02f, az + nz * wA, MAT_PAINT)
                addV(out, ax - nx * wA, 0.02f, az - nz * wA, MAT_PAINT)
                addV(out, bx - nx * wB, 0.02f, bz - nz * wB, MAT_PAINT)
                addV(out, ax + nx * wA, 0.02f, az + nz * wA, MAT_PAINT)
                addV(out, bx - nx * wB, 0.02f, bz - nz * wB, MAT_PAINT)
                addV(out, bx + nx * wB, 0.02f, bz + nz * wB, MAT_PAINT)
                i += 3
            }
        }
        return if (out.isEmpty()) null else Mesh(out.toFloatArray())
    }

    /** 树：方柱树干 + 3 层缩小的球冠（八面体细分），比单锥自然。 */
    private fun buildTrees(): Pair<Mesh, Mesh> {
        val crowns = ArrayList<Float>()
        val trunks = ArrayList<Float>()
        var seed = 987654321
        fun rnd(): Float {
            seed = seed * 1103515245 + 12345
            return ((seed shr 16) and 0x7FFF) / 32767f
        }
        // 严格按 [TREE_PERIOD] 重复：滚动量取"里程 mod TREE_PERIOD"，平移一个周期后
        // 几何与之前完全重合，视觉上连续。随机只作用在横向位置和高度上，不影响 z 周期。
        // 两排树：近排贴着路边，远排更高更远，增加纵深。
        // 两个硬约束：
        //  1) z 必须严格等于 k*TREE_PERIOD+起偏移，不能加随机量——滚动量取
        //     "里程 mod TREE_PERIOD"，第 k 棵必须正好落到第 k-1 棵的位置，否则会跳。
        //  2) 起偏移取 16m：相机在 z≈-9、朝 +z 看，z=0 的树横向 x≈±10~19 时水平角接近
        //     50°，已经偏出视野；从 16m 起才落在画面内。
        // 高度也压低了：相机 y≈6.2、俯视，树高一超过 7m 树冠就跑到画面外，
        // 只剩一根绿杆（实测绿色像素只有 22 个）。
        var k = -4
        while (k * TREE_PERIOD < 240f) {
            val zBase = k * TREE_PERIOD + 16f
            for (side in intArrayOf(-1, 1)) {
                // 近排：贴着路边（横向收紧，否则相机偏向左侧时右排整列出不了画面）
                plantTree(crowns, trunks, side * (8.0f + rnd() * 4f), zBase, 4.4f + rnd() * 2.0f)
                // 远排：错开半个周期，更高更远，补纵深
                plantTree(crowns, trunks, side * (14f + rnd() * 6f),
                    zBase + TREE_PERIOD * 0.5f, 5.2f + rnd() * 2.4f)
            }
            k++
        }
        return Mesh(crowns.toFloatArray()) to Mesh(trunks.toFloatArray())
    }

    /** 一棵树：树干进 [trunks]（棕褐）、3 层球冠进 [crowns]（绿），两者颜色分开画。 */
    private fun plantTree(
        crowns: MutableList<Float>, trunks: MutableList<Float>,
        x: Float, z: Float, h: Float,
    ) {
        boxInto(trunks, x - 0.16f, 0f, z - 0.16f, x + 0.16f, h * 0.58f, z + 0.16f, MAT_LEAF)
        val baseY = h * 0.42f
        sphere(crowns, x, baseY + h * 0.18f, z, h * 0.30f)
        sphere(crowns, x, baseY + h * 0.40f, z, h * 0.24f)
        sphere(crowns, x, baseY + h * 0.58f, z, h * 0.16f)
    }

    /** 路灯：立在路右沿，顶部一根横臂 + 灯头。 */
    private fun buildLamps(): Mesh {
        val out = ArrayList<Float>()
        // 同样按 [LAMP_PERIOD] 严格重复，服务滚动
        var k = -2
        while (k * LAMP_PERIOD < 240f) {
            val z = k * LAMP_PERIOD
            val x = 5.6f
            boxInto(out, x - 0.09f, 0f, z - 0.09f, x + 0.09f, 7.0f, z + 0.09f, MAT_LAMP)
            boxInto(out, x - 1.7f, 6.9f, z - 0.08f, x, 7.08f, z + 0.08f, MAT_LAMP)
            boxInto(out, x - 1.85f, 6.72f, z - 0.16f, x - 1.35f, 6.92f, z + 0.16f, MAT_LIGHT)
            k++
        }
        return Mesh(out.toFloatArray())
    }

    /** 接地阴影：椭圆盘，中心最暗、边缘透明。 */
    private fun buildShadow(): GlowMesh {
        val seg = 40
        val v = ArrayList<Float>(seg * 18)
        val rx = 1.55f
        val rz = 2.9f
        for (s in 0 until seg) {
            val a0 = 2.0 * Math.PI * s / seg
            val a1 = 2.0 * Math.PI * (s + 1) / seg
            val c0 = cos(a0).toFloat(); val s0 = sin(a0).toFloat()
            val c1 = cos(a1).toFloat(); val s1 = sin(a1).toFloat()
            addGlow(v, 0f, 0f, 0f, 0.62f)
            addGlow(v, rx * c0, 0f, rz * s0, 0.0f)
            addGlow(v, rx * c1, 0f, rz * s1, 0.0f)
        }
        return GlowMesh(v.toFloatArray())
    }

    private fun buildRing(inner: Float, outer: Float, peak: Float, minAlpha: Float): GlowMesh {
        val seg = 48
        val v = ArrayList<Float>(seg * 18)
        for (s in 0 until seg) {
            val a0 = 2.0 * Math.PI * s / seg
            val a1 = 2.0 * Math.PI * (s + 1) / seg
            val c0 = cos(a0).toFloat(); val s0 = sin(a0).toFloat()
            val c1 = cos(a1).toFloat(); val s1 = sin(a1).toFloat()
            addGlow(v, inner * c0, 0f, inner * s0, minAlpha)
            addGlow(v, outer * c0, 0f, outer * s0, peak)
            addGlow(v, outer * c1, 0f, outer * s1, peak)
            addGlow(v, inner * c0, 0f, inner * s0, minAlpha)
            addGlow(v, outer * c1, 0f, outer * s1, peak)
            addGlow(v, inner * c1, 0f, inner * s1, minAlpha)
        }
        return GlowMesh(v.toFloatArray())
    }

    private fun buildRoadGlow(): GlowMesh {
        val v = ArrayList<Float>()
        val halfW = 2.4f
        val z0 = -3f
        val z1 = 34f
        val steps = 12
        for (i in 0 until steps) {
            val t0 = i.toFloat() / steps
            val t1 = (i + 1).toFloat() / steps
            val za = z0 + (z1 - z0) * t0
            val zb = z0 + (z1 - z0) * t1
            val aa = 0.75f * (1f - t0) * (1f - t0)
            val ab = 0.75f * (1f - t1) * (1f - t1)
            val wa = halfW * (1f - 0.35f * t0)
            val wb = halfW * (1f - 0.35f * t1)
            addGlow(v, -wa, 0f, za, aa); addGlow(v, wa, 0f, za, aa); addGlow(v, wb, 0f, zb, ab)
            addGlow(v, -wa, 0f, za, aa); addGlow(v, wb, 0f, zb, ab); addGlow(v, -wb, 0f, zb, ab)
        }
        return GlowMesh(v.toFloatArray())
    }

    /** 八面体细分一次，18 面，当球冠用。 */
    /**
     * 球冠：八面体细分一次（8 面 → 32 面）。
     *
     * 法线必须用"球心指向顶点"的方向（走 addN）。之前用 addV 的话法线统一是 (0,1,0)，
     * 整个球每个面亮度相同，只看得到轮廓——树冠会变成一块扁平的绿色多边形。
     */
    private fun sphere(out: MutableList<Float>, cx: Float, cy: Float, cz: Float, r: Float) {
        val base = arrayOf(
            floatArrayOf(0f, 1f, 0f, 1f, 0f, 0f, 0f, 0f, 1f),
            floatArrayOf(0f, 1f, 0f, 0f, 0f, 1f, -1f, 0f, 0f),
            floatArrayOf(0f, 1f, 0f, -1f, 0f, 0f, 0f, 0f, -1f),
            floatArrayOf(0f, 1f, 0f, 0f, 0f, -1f, 1f, 0f, 0f),
            floatArrayOf(0f, -1f, 0f, 0f, 0f, 1f, 1f, 0f, 0f),
            floatArrayOf(0f, -1f, 0f, -1f, 0f, 0f, 0f, 0f, 1f),
            floatArrayOf(0f, -1f, 0f, 0f, 0f, -1f, -1f, 0f, 0f),
            floatArrayOf(0f, -1f, 0f, 1f, 0f, 0f, 0f, 0f, -1f),
        )
        fun nv(ax: Float, ay: Float, az: Float) {
            val len = kotlin.math.sqrt(ax * ax + ay * ay + az * az).coerceAtLeast(1e-6f)
            addN(out, cx + ax * r, cy + ay * r, cz + az * r,
                ax / len, ay / len, az / len, MAT_LEAF)
        }
        fun mid(ax: Float, ay: Float, az: Float, bx: Float, by: Float, bz: Float): FloatArray {
            val mx = (ax + bx) * 0.5f
            val my = (ay + by) * 0.5f
            val mz = (az + bz) * 0.5f
            val l = kotlin.math.sqrt(mx * mx + my * my + mz * mz).coerceAtLeast(1e-6f)
            return floatArrayOf(mx / l, my / l, mz / l)
        }
        for (t in base) {
            val ax = t[0]; val ay = t[1]; val az = t[2]
            val bx = t[3]; val by = t[4]; val bz = t[5]
            val gx = t[6]; val gy = t[7]; val gz = t[8]
            val mab = mid(ax, ay, az, bx, by, bz)
            val mbc = mid(bx, by, bz, gx, gy, gz)
            val mca = mid(gx, gy, gz, ax, ay, az)
            nv(ax, ay, az); nv(mab[0], mab[1], mab[2]); nv(mca[0], mca[1], mca[2])
            nv(bx, by, bz); nv(mbc[0], mbc[1], mbc[2]); nv(mab[0], mab[1], mab[2])
            nv(gx, gy, gz); nv(mca[0], mca[1], mca[2]); nv(mbc[0], mbc[1], mbc[2])
            nv(mab[0], mab[1], mab[2]); nv(mbc[0], mbc[1], mbc[2]); nv(mca[0], mca[1], mca[2])
        }
    }

    private fun addGlow(v: MutableList<Float>, x: Float, y: Float, z: Float, a: Float) {
        v.add(x); v.add(y); v.add(z); v.add(a)
    }

    private fun addV(out: MutableList<Float>, x: Float, y: Float, z: Float, mat: Mat) {
        // 球/带这类几何不做精确法线，统一给朝上（顶面受光最多）
        out.add(x); out.add(y); out.add(z)
        out.add(0f); out.add(1f); out.add(0f)
        out.add(mat.spec); out.add(mat.gloss)
    }

    private fun addN(
        v: MutableList<Float>, x: Float, y: Float, z: Float,
        nx: Float, ny: Float, nz: Float, mat: Mat,
    ) {
        v.add(x); v.add(y); v.add(z)
        v.add(nx); v.add(ny); v.add(nz)
        v.add(mat.spec); v.add(mat.gloss)
    }

    private fun box(
        x0: Float, y0: Float, z0: Float, x1: Float, y1: Float, z1: Float, mat: Mat,
    ): Mesh {
        val v = ArrayList<Float>(36 * FPV)
        boxInto(v, x0, y0, z0, x1, y1, z1, mat)
        return Mesh(v.toFloatArray())
    }

    private fun boxInto(
        v: MutableList<Float>, x0: Float, y0: Float, z0: Float,
        x1: Float, y1: Float, z1: Float, mat: Mat,
    ) {
        fun q(
            ax: Float, ay: Float, az: Float, bx: Float, by: Float, bz: Float,
            cx: Float, cy: Float, cz: Float, dx: Float, dy: Float, dz: Float,
            nx: Float, ny: Float, nz: Float,
        ) {
            addN(v, ax, ay, az, nx, ny, nz, mat); addN(v, bx, by, bz, nx, ny, nz, mat)
            addN(v, cx, cy, cz, nx, ny, nz, mat)
            addN(v, ax, ay, az, nx, ny, nz, mat); addN(v, cx, cy, cz, nx, ny, nz, mat)
            addN(v, dx, dy, dz, nx, ny, nz, mat)
        }
        q(x0, y1, z1, x1, y1, z1, x1, y1, z0, x0, y1, z0, 0f, 1f, 0f)
        q(x0, y0, z0, x1, y0, z0, x1, y0, z1, x0, y0, z1, 0f, -1f, 0f)
        q(x0, y0, z1, x1, y0, z1, x1, y1, z1, x0, y1, z1, 0f, 0f, 1f)
        q(x0, y1, z0, x1, y1, z0, x1, y0, z0, x0, y0, z0, 0f, 0f, -1f)
        q(x1, y0, z1, x1, y0, z0, x1, y1, z0, x1, y1, z1, 1f, 0f, 0f)
        q(x0, y0, z0, x0, y0, z1, x0, y1, z1, x0, y1, z0, -1f, 0f, 0f)
    }

    private fun buildWheels(): Mesh {
        val v = ArrayList<Float>()
        val r = 0.34f
        val halfW = 0.13f
        for ((cx, cz) in listOf(
            -0.86f to -1.46f, 0.86f to -1.46f,
            -0.86f to 1.50f, 0.86f to 1.50f,
        )) {
            val sides = 10
            for (s in 0 until sides) {
                val a0 = 2.0 * Math.PI * s / sides
                val a1 = 2.0 * Math.PI * (s + 1) / sides
                val y0 = (r * sin(a0)).toFloat(); val z0 = (r * cos(a0)).toFloat()
                val y1 = (r * sin(a1)).toFloat(); val z1 = (r * cos(a1)).toFloat()
                val nx0 = sin(a0).toFloat(); val nz0 = cos(a0).toFloat()
                val nx1 = sin(a1).toFloat(); val nz1 = cos(a1).toFloat()
                addN(v, cx + halfW, r + y0, cz + z0, nx0, 0f, nz0, MAT_TIRE)
                addN(v, cx - halfW, r + y1, cz + z1, nx1, 0f, nz1, MAT_TIRE)
                addN(v, cx + halfW, r + y1, cz + z1, nx1, 0f, nz1, MAT_TIRE)
                addN(v, cx + halfW, r + y0, cz + z0, nx0, 0f, nz0, MAT_TIRE)
                addN(v, cx - halfW, r + y0, cz + z0, nx0, 0f, nz0, MAT_TIRE)
                addN(v, cx - halfW, r + y1, cz + z1, nx1, 0f, nz1, MAT_TIRE)
            }
        }
        return Mesh(v.toFloatArray())
    }

    private fun quad(
        x0: Float, y0: Float, z0: Float, x1: Float, y1: Float, z1: Float,
        x2: Float, y2: Float, z2: Float, x3: Float, y3: Float, z3: Float,
        nx: Float, ny: Float, nz: Float, mat: Mat,
    ): Mesh {
        val v = ArrayList<Float>(6 * FPV)
        addN(v, x0, y0, z0, nx, ny, nz, mat); addN(v, x1, y1, z1, nx, ny, nz, mat)
        addN(v, x2, y2, z2, nx, ny, nz, mat)
        addN(v, x0, y0, z0, nx, ny, nz, mat); addN(v, x2, y2, z2, nx, ny, nz, mat)
        addN(v, x3, y3, z3, nx, ny, nz, mat)
        return Mesh(v.toFloatArray())
    }

    companion object {
        fun releaseAll() {
            GLES20.glUseProgram(0)
        }
    }
}



/**
 * 车模一组几何的**纯数据**（普通堆数组，不是 DirectByteBuffer）。
 *
 * 为什么不用 direct buffer：222,554 顶点按 32 字节/顶点要 7.1 MB 堆外内存，
 * 加上 3.75 MB 的原始 byte[] 和 1.7 MB 索引，合计 12.6 MB。鸿蒙容器的 direct
 * memory 限额撑不住，而且每次重进面板都重建一遍又不释放，累积后直接把进程打死。
 * 改成普通数组交给 GL 线程上传到 VBO（显存），堆外只留显存那一份。
 */
/** Models with a shared tail/brake surface keep a dim running light until brake telemetry is true. */
internal fun tailLampIntensity(
    authoredMaterial: Boolean,
    lampRole: Int,
    hasSeparateBrakeLamp: Boolean,
    brakeLights: Boolean?,
): Float = if (authoredMaterial && lampRole == 7 && !hasSeparateBrakeLamp && brakeLights != true) 0.2f else 1f

internal fun usesTurnLampOverlay(authoredMaterial: Boolean, lampRole: Int): Boolean =
    authoredMaterial && lampRole in 1..2

internal fun isTurnLampOverlayActive(
    authoredMaterial: Boolean, lampRole: Int,
    leftBlinker: Boolean?, rightBlinker: Boolean?, phaseOn: Boolean,
): Boolean = usesTurnLampOverlay(authoredMaterial, lampRole) && phaseOn &&
    (if (lampRole == 1) leftBlinker else rightBlinker) == true

// Source vehicle's lit overlay is lifted 1.856 mm along the authored normal, under the same hinge transform.
internal const val TURN_LAMP_NORMAL_OFFSET = 0.001856f

/** Preserve occlusion by the vehicle while adding emission independently of the lens material opacity. */
internal inline fun drawTurnLampOverlayPass(uOverlay: Int, uNormalOffset: Int, draw: () -> Unit) {
    GLES20.glEnable(GLES20.GL_DEPTH_TEST)
    GLES20.glDepthFunc(GLES20.GL_LEQUAL)
    GLES20.glDepthMask(false)
    GLES20.glEnable(GLES20.GL_BLEND)
    // Match the source overlay: duplicate authored faces must not add emission repeatedly.
    GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA)
    GLES20.glEnable(GLES20.GL_POLYGON_OFFSET_FILL)
    GLES20.glPolygonOffset(0f, -2f)
    GLES20.glUniform1f(uOverlay, 1f)
    GLES20.glUniform1f(uNormalOffset, TURN_LAMP_NORMAL_OFFSET)
    try {
        draw()
    } finally {
        GLES20.glUniform1f(uOverlay, 0f)
        GLES20.glUniform1f(uNormalOffset, 0f)
        GLES20.glPolygonOffset(0f, 0f)
        GLES20.glDisable(GLES20.GL_POLYGON_OFFSET_FILL)
        GLES20.glDepthMask(true)
        GLES20.glDisable(GLES20.GL_BLEND)
        GLES20.glDepthFunc(GLES20.GL_LEQUAL)
    }
}

internal class CarMaterial(
    val rgba: FloatArray,
    val spec: Float,
    val gloss: Float,
    val textureIndex: Int,
    val flags: Int,
    val alphaCutoff: Float,
) {
    val isGlass: Boolean get() = flags and 1 != 0
    val isTransparent: Boolean get() = flags and 2 != 0
    val alphaMode: Int get() = if (isTransparent) 1 else if (flags and 4 != 0) 2 else 0
}

/** Encoded images are shared by all groups and decoded only when the GL context uploads them. */
internal class CarTextureTable(val images: List<ByteArray>)

internal class CarData(
    val name: String,
    val verts: FloatArray,      // pos(3) + normal(3)，每顶点 6 个 float
    val indices: ShortArray,
    /** 可动件类型：0=车身 1..4=左前/右前/左后/右后门 5=引擎盖 6=尾门 */
    val rigKind: Int = 0,
    /** 铰链轴向：0=X 1=Y */
    val hingeAxis: Int = 1,
    /** 打开时绕铰链转过的角度（带符号，度） */
    val angleDeg: Float = 0f,
    /** 灯角色：0=非灯 1=左转 2=右转 3=刹车 4=倒车 5=日行 6=示宽 7=尾灯 8=前照灯(v6) */
    val lampRole: Int = 0,
    /** 铰链点（车身局部坐标） */
    val pivot: FloatArray = ZERO3,
    val uv: FloatArray = FloatArray(0),
    val material: CarMaterial? = null,
) {
    var textureTable: CarTextureTable? = null
        internal set
    companion object { private val ZERO3 = floatArrayOf(0f, 0f, 0f) }
    /**
     * 玻璃要透出内饰，所以走半透明混合。
     * **灯具必须排除在外**：灯罩材质名也是 Car_Glass_*，走 alpha 0.42 混合会把
     * 自发光稀释成浅粉色（真机实测 RGB 231,157,159），看不出灯在亮。
     */
    val isGlass: Boolean = material?.isGlass ?: ((name.contains("Glass") || name.contains("Mirror") ||
        name.contains("Window")) && lampRole == 0)
    val isTransparent: Boolean = material?.isTransparent ?: isGlass
    /** 灯具自发光：由 lampRole 判定，不靠名字猜 */
    val isLamp: Boolean = lampRole != 0
    val alpha: Float = material?.rgba?.get(3) ?: if (isGlass) 0.42f else 1.0f

    // 灯底色调深：自发光叠上去才有对比，浅色底会把红光冲成粉色
    val color: FloatArray = material?.rgba ?: if (lampRole != 0) DARK_LAMP else carColor(name)
    // 玻璃高光更锐（光面），车漆次之；内饰/杂项哑光，不产生乱反射
    val spec: Float = material?.spec ?: if (isGlass) 0.90f
        else if (name.contains("Paint")) 0.65f else 0.06f
    val gloss: Float = material?.gloss ?: if (isGlass) 0.95f
        else if (name.contains("Paint")) 0.78f else 0.15f
}

// ── 环境滚动周期（米）。几何按这些间隔严格重复，滚动量取"里程 mod 周期" ──
private const val DEBUG_MAT = false
private const val TREE_PERIOD = 9f
private const val LAMP_PERIOD = 24f
private const val DASH_PERIOD = 9f      // 4m 实线段 + 5m 空隙
private const val SEAM_PERIOD = 12f     // 路面接缝

/** 灯具的底色：暗，让自发光出头 */
private val DARK_LAMP = floatArrayOf(0.15f, 0.145f, 0.15f)

/** Disable every textured-material state before drawing untextured environment geometry. */
internal fun resetCarTextureState(aUv: Int, uUseTexture: Int, uAlphaMode: Int, uAlphaCutoff: Int) {
    if (aUv >= 0) {
        GLES20.glDisableVertexAttribArray(aUv)
        GLES20.glVertexAttrib2f(aUv, 0f, 0f)
    }
    GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
    GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, 0)
    GLES20.glUniform1f(uUseTexture, 0f)
    GLES20.glUniform1i(uAlphaMode, 0)
    GLES20.glUniform1f(uAlphaCutoff, 0.5f)
}

/** One texture upload per image per EGL context, shared across doors and material groups. */
internal class CarTextureCache(private val uploadImage: (ByteArray, Int) -> Int = ::uploadCarTexture) {
    private val handles = java.util.IdentityHashMap<CarTextureTable, IntArray>()

    fun get(table: CarTextureTable?, index: Int): Int {
        if (table == null || index !in table.images.indices) return 0
        val ids = handles.getOrPut(table) { IntArray(table.images.size) { -1 } }
        if (ids[index] >= 0) return ids[index]
        ids[index] = 0 // A failed image is reported once and falls back to its material color.
        ids[index] = uploadImage(table.images[index], index)
        return ids[index]
    }

    fun release() {
        for (ids in handles.values) for (id in ids) {
            if (id > 0) GLES20.glDeleteTextures(1, intArrayOf(id), 0)
        }
        handles.clear()
    }

    fun forgetContext() { handles.clear() }
}

private fun uploadCarTexture(bytes: ByteArray, index: Int): Int {
    var id = 0
    try {
            val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size,
            BitmapFactory.Options().apply { inScaled = false; inPremultiplied = false })
            ?: error("Cannot decode car texture $index")
        try {
            val generated = IntArray(1)
            GLES20.glGenTextures(1, generated, 0)
            id = generated[0]
            check(id != 0) { "No GL texture handle for car texture $index" }
            GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, id)
            val powerOfTwo = bitmap.width > 0 && bitmap.height > 0 &&
                (bitmap.width and (bitmap.width - 1)) == 0 && (bitmap.height and (bitmap.height - 1)) == 0
            val wrap = if (powerOfTwo) GLES20.GL_REPEAT else GLES20.GL_CLAMP_TO_EDGE
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_S, wrap)
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_WRAP_T, wrap)
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
            GLES20.glTexParameteri(GLES20.GL_TEXTURE_2D, GLES20.GL_TEXTURE_MIN_FILTER,
                if (powerOfTwo) GLES20.GL_LINEAR_MIPMAP_LINEAR else GLES20.GL_LINEAR)
            // glTF UVs address the top image row at v=0; Bitmap pixels need no V flip.
            GLUtils.texImage2D(GLES20.GL_TEXTURE_2D, 0, bitmap, 0)
            if (powerOfTwo) GLES20.glGenerateMipmap(GLES20.GL_TEXTURE_2D)
            val error = GLES20.glGetError()
            check(error == GLES20.GL_NO_ERROR) { "Car texture $index upload GL error $error" }
            return id
        } finally { bitmap.recycle() }
    } catch (failure: Throwable) {
        if (id != 0) GLES20.glDeleteTextures(1, intArrayOf(id), 0)
        CrashLog.record("car texture $index", failure)
    }
    return 0
}

/** 一组几何在 GPU 上的形态：两个 VBO。只在 GL 线程创建和释放。 */
internal class CarPart(private val data: CarData, private val textures: CarTextureCache? = null) {
    private var vbo = 0
    private var ibo = 0
    private var built = false

    val name: String get() = data.name
    val color: FloatArray get() = data.color
    val isGlass: Boolean get() = data.isGlass
    val isTransparent: Boolean get() = data.isTransparent
    val hasAuthoredMaterial: Boolean get() = data.material != null
    val center = FloatArray(3).also { out ->
        if (data.verts.isNotEmpty()) for (axis in 0..2) {
            var lo = Float.POSITIVE_INFINITY; var hi = Float.NEGATIVE_INFINITY
            for (i in axis until data.verts.size step 6) {
                lo = kotlin.math.min(lo, data.verts[i]); hi = kotlin.math.max(hi, data.verts[i])
            }
            out[axis] = (lo + hi) * 0.5f
        }
    }
    val triangleCount: Int get() = data.indices.size / 3
    val rigKind: Int get() = data.rigKind
    val hingeAxis: Int get() = data.hingeAxis
    val angleDeg: Float get() = data.angleDeg
    val lampRole: Int get() = data.lampRole
    val pivot: FloatArray get() = data.pivot
    val alpha: Float get() = data.alpha
    val isLamp: Boolean get() = data.lampRole != 0

    private val hasUv: Boolean get() = data.uv.size == data.verts.size / 3
    private val stride: Int get() = if (hasUv) 32 else 24

    /** 必须在 GL 线程调用一次。v6 顶点上传为 pos + normal + UV（32 字节）。 */
    fun build() {
        if (built) return
        val vb = IntArray(1)
        GLES20.glGenBuffers(1, vb, 0)
        vbo = vb[0]
        val byteCount = data.verts.size / 6 * stride
        val vBuf = ByteBuffer.allocateDirect(byteCount)
            .order(ByteOrder.nativeOrder()).asFloatBuffer()
        if (hasUv) {
            for (v in 0 until data.verts.size / 6) {
                vBuf.put(data.verts, v * 6, 6)
                vBuf.put(data.uv, v * 2, 2)
            }
        } else vBuf.put(data.verts)
        vBuf.position(0)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo)
        GLES20.glBufferData(
            GLES20.GL_ARRAY_BUFFER, byteCount, vBuf, GLES20.GL_STATIC_DRAW,
        )

        val ib = IntArray(1)
        GLES20.glGenBuffers(1, ib, 0)
        ibo = ib[0]
        val iBuf = ByteBuffer.allocateDirect(data.indices.size * 2)
            .order(ByteOrder.nativeOrder()).asShortBuffer()
        iBuf.put(data.indices).position(0)
        GLES20.glBindBuffer(GLES20.GL_ELEMENT_ARRAY_BUFFER, ibo)
        GLES20.glBufferData(
            GLES20.GL_ELEMENT_ARRAY_BUFFER, data.indices.size * 2, iBuf, GLES20.GL_STATIC_DRAW,
        )
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
        GLES20.glBindBuffer(GLES20.GL_ELEMENT_ARRAY_BUFFER, 0)
        built = true
    }

    fun draw(aPos: Int, aNormal: Int, aMat: Int, uAlpha: Int = -1,
        aUv: Int = -1, uUseTexture: Int = -1, uTexture: Int = -1,
        uAlphaMode: Int = -1, uAlphaCutoff: Int = -1,
    ) {
        if (!built) build()
        // 材质参数是每组的常量，用常量顶点属性喂，不占顶点带宽
        // 环境 Mesh.draw 会启用 aMat 数组；必须关闭，否则常量被忽略，继续读旧 VBO。
        GLES20.glDisableVertexAttribArray(aMat)
        GLES20.glVertexAttrib2f(aMat, data.spec, data.gloss)
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, vbo)
        GLES20.glVertexAttribPointer(aPos, 3, GLES20.GL_FLOAT, false, stride, 0)
        GLES20.glEnableVertexAttribArray(aPos)
        GLES20.glVertexAttribPointer(aNormal, 3, GLES20.GL_FLOAT, false, stride, 12)
        GLES20.glEnableVertexAttribArray(aNormal)
        if (uAlpha >= 0) GLES20.glUniform1f(uAlpha, data.alpha)
        if (aUv >= 0) {
            GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
            val texture = if (hasUv) textures?.get(data.textureTable, data.material?.textureIndex ?: -1) ?: 0 else 0
            GLES20.glBindTexture(GLES20.GL_TEXTURE_2D, texture)
            GLES20.glUniform1f(uUseTexture, if (texture != 0) 1f else 0f)
            GLES20.glUniform1i(uTexture, 0)
            GLES20.glUniform1i(uAlphaMode, data.material?.alphaMode ?: if (data.isGlass) 1 else 0)
            GLES20.glUniform1f(uAlphaCutoff, data.material?.alphaCutoff ?: 0.5f)
            if (texture != 0) {
                GLES20.glVertexAttribPointer(aUv, 2, GLES20.GL_FLOAT, false, stride, 24)
                GLES20.glEnableVertexAttribArray(aUv)
            } else {
                GLES20.glDisableVertexAttribArray(aUv)
                GLES20.glVertexAttrib2f(aUv, 0f, 0f)
            }
        }
        GLES20.glBindBuffer(GLES20.GL_ELEMENT_ARRAY_BUFFER, ibo)
        GLES20.glDrawElements(
            GLES20.GL_TRIANGLES, data.indices.size, GLES20.GL_UNSIGNED_SHORT, 0,
        )
        GLES20.glBindBuffer(GLES20.GL_ARRAY_BUFFER, 0)
        GLES20.glBindBuffer(GLES20.GL_ELEMENT_ARRAY_BUFFER, 0)
    }

    fun release() {
        if (vbo != 0) { GLES20.glDeleteBuffers(1, intArrayOf(vbo), 0); vbo = 0 }
        if (ibo != 0) { GLES20.glDeleteBuffers(1, intArrayOf(ibo), 0); ibo = 0 }
        built = false
    }
}

/**
 * 只保留从车外看得见的部件。
 *
 * 原模型 29 万三角形里，内饰（座椅皮革 5.2 万、内部镀铬 4.1 万、地毯布料、发动机舱）
 * 占了 17 万多。这些面在 196dp 高的面板上每个只有一两个像素，渲染出来就是密密麻麻的
 * 三角形噪点，而且从车外根本看不到 —— 直接不画。
 */
private fun isExterior(@Suppress("UNUSED_PARAMETER") name: String): Boolean = true

/**
 * 按材质名挑颜色：这台车是白色车漆 + 深色玻璃。
 * 内饰（座椅/地毯/仪表台/车内镀铬）+ 外饰杂项**统一深灰**：29 万三角形里这部分
 * 占 17 万+，在 200dp 高的面板上每个面只有一两个像素，按材质分别着色只会得到
 * 三角形噪点；统一色隔着半透明玻璃看是干净的深色座舱。
 */
internal fun carColor(name: String): FloatArray = when {
    name.contains("Paint") -> floatArrayOf(0.95f, 0.96f, 0.97f)
    // 白天玻璃是淡蓝灰、半透明
    name.contains("Glass") -> floatArrayOf(0.62f, 0.74f, 0.82f)
    name.contains("Wheel") -> floatArrayOf(0.045f, 0.045f, 0.05f)
    name.contains("Lamp") || name.contains("Light") ->
        floatArrayOf(0.94f, 0.95f, 0.92f)
    else -> floatArrayOf(0.20f, 0.21f, 0.23f)
}

/**
 * 读 assets/car.bin（CARM v3–v6），产出纯数据。
 * 在 IO 线程调用；不碰任何 GL 资源，也不留 DirectByteBuffer 常驻。
 */
internal object CarAssets {
    /**
     * 位置量化精度。0.5mm 只抓得住"顶点完全重合"的重复面。
     * 试过放宽到 2mm（8.9.43）想让"间距 1~3mm 的近邻平行面"也被判定为重复，
     * 实测车尾尾板高频只从 10.53 降到 9.88（−6%），收益太小而误删真实几何的风险变大，
     * 已回退。车尾尾板那 8855 片 ChromePlastic + 5096 片 Chrome 的双层问题另想办法。
     */
    private const val Q = 2000f

    /** car.bin 的顶点格式：pos(3f) + normal(3f) = 6 个 float（与环境几何的 FPV=8 不同） */
    private const val VERT_FLOATS = 6

    /**
     * 剔除「同一位置、同一朝向」的重复三角形。
     *
     * 为什么必须在几何层做：实测车尾 794 个「位置+朝向」格里有 783 个是**同材质**
     * 内部重叠（车尾下护板整片 Car_ChromePlastic 5978 片），前门窗框还有
     * 玻璃×车漆 153 对、镀铬×车漆 57 对等跨材质共面。
     * 重复面之间的深度差在浮点误差量级，GL_LEQUAL 与 GL_LESS 都会逐像素翻转成斑块，
     * 24 位深度、near/far 收缩、polygon offset 全部无效（都试过）。
     *
     * 保留策略：按材质深度层级升序（越外先占位），后遇到的重复片直接丢掉。
     */
    private fun dedupeTriangles(groups: MutableList<CarData>) {
        var dropped = 0
        // **只在同一可动件内部去重**。跨 rigKind 的重合是"关闭状态下贴合"：
        // 尾门关着时它和车身的面位置完全重合，若跨 kind 去重就会删掉尾门那一片，
        // 一打开尾门就露出破洞 —— 8.9.41 真机就是这么翻车的。
        val byKind = groups.indices.groupBy { groups[it].rigKind }
        for ((_, kindIdx) in byKind) {
            val seen = HashSet<Long>(1 shl 17)
            val order = kindIdx.sortedBy { depthLayerOf(groups[it].name) }
            for (gi in order) {
                val g = groups[gi]
                val v = g.verts
                val src = g.indices
                val nv = v.size / 6
                val keep = ShortArray(src.size)
                var kn = 0
                var t = 0
                while (t + 2 < src.size) {
                    val a = src[t].toInt() and 0xFFFF
                    val b = src[t + 1].toInt() and 0xFFFF
                    val c = src[t + 2].toInt() and 0xFFFF
                    if (a < nv && b < nv && c < nv) {
                        if (seen.add(triSig(v, a, b, c))) {
                            keep[kn] = src[t]; keep[kn + 1] = src[t + 1]; keep[kn + 2] = src[t + 2]
                            kn += 3
                        } else {
                            dropped += 3
                        }
                    }
                    t += 3
                }
                if (kn != src.size) {
                    groups[gi] = CarData(
                        name = g.name,
                        verts = g.verts,
                        indices = keep.copyOf(kn),
                        rigKind = g.rigKind,
                        hingeAxis = g.hingeAxis,
                        angleDeg = g.angleDeg,
                        lampRole = g.lampRole,
                        pivot = g.pivot,
                    )
                }
            }
        }
        android.util.Log.i("VehicleScene3D", "dedupe: dropped ${dropped / 3} duplicate triangles")
    }

    private fun q(v: Float): Long = Math.round(v * Q).toLong() and 0x1FFFFFL

    /** 三个顶点量化后按字典序排序再混合，保证顶点书写顺序不同也能识别为同一片 */
    private fun triSig(v: FloatArray, a: Int, b: Int, c: Int): Long {
        fun pack(i: Int): Long {
            val o = i * 6
            return (q(v[o]) shl 42) or (q(v[o + 1]) shl 21) or q(v[o + 2])
        }
        var x = pack(a); var y = pack(b); var z = pack(c)
        if (x > y) { val t = x; x = y; y = t }
        if (y > z) { val t = y; y = z; z = t }
        if (x > y) { val t = x; x = y; y = t }
        return x * 0x9E3779B97F4A7C15uL.toLong() + y * 31 + z
    }

    /** 与渲染侧 depthLayer 保持一致的材质层级（越外越小） */
    private fun depthLayerOf(name: String): Int = when {
        name.contains("Internal") -> 6
        name.contains("ChromePlastic") -> 0
        name.contains("Chrome") -> 1
        name.contains("Paint") -> 2
        name.contains("Glass") || name.contains("Mirror") || name.contains("Window") -> 3
        name.contains("Leather") -> 4
        name.contains("Cloth") || name.contains("Fabric") -> 5
        name == "#bcbcbc" -> 7
        name == "#707070" -> 8
        name == "#454545" -> 9
        else -> 10
    }

    /**
     * 贯穿尾灯带的中间段必须跟着尾门走。
     *
     * car.bin 里 `LampTail` 整条都挂在 body 上（x 从 −0.84 到 +0.85），可尾门开口只有
     * x ±0.709 —— 也就是说这条灯带中间约 100 片三角形其实长在尾门上。归 body 的后果很直接：
     * 掀尾门时整条灯带动也不动，用户看到的就是"后背厢打开了但贯穿灯没变化"。
     *
     * 实测分布（三角形中心 x）：两端集中在 |x|≈0.70~0.85（各 40~57 片，是车身侧），
     * 中间 |x|<0.70 每档约 2 片，合计约 100 片，正对尾门开口。边界和开口完全吻合，
     * 所以直接按"中心 x 是否落在尾门 x 跨度内"切分即可。
     *
     * 顶点数组不动，只重建索引（同 8.9.41 的做法，避免破坏已算好的平滑法线）。
     * 铰链参数沿用尾门自己的，同一次导出必然一致。
     */
    private fun reassignTailBandToTrunk(groups: MutableList<CarData>) {
        val trunks = groups.filter { it.rigKind == 6 }
        if (trunks.isEmpty()) return
        val hinge = trunks.first()
        // 尾门开口宽度：优先用尾门外板（车漆件），拿不到就退回所有尾门组的并集
        val shell = trunks.filter { it.name.contains("Paint") }.ifEmpty { trunks }
        var xMin = Float.MAX_VALUE
        var xMax = -Float.MAX_VALUE
        var zMin = Float.MAX_VALUE
        var zMax = -Float.MAX_VALUE
        for (g in shell) {
            var v = 0
            while (v < g.verts.size) {
                val x = g.verts[v]
                val z = g.verts[v + 2]
                if (x < xMin) xMin = x
                if (x > xMax) xMax = x
                if (z < zMin) zMin = z
                if (z > zMax) zMax = z
                v += VERT_FLOATS
            }
        }
        if (xMin > xMax || zMin > zMax) return

        val added = ArrayList<CarData>(2)
        for (g in groups.toList()) {
            // 只处理挂在车身上的灯组（尾灯/刹车灯带）
            if (g.rigKind != 0 || g.lampRole == 0) continue
            val inner = ArrayList<Short>(g.indices.size)
            val outer = ArrayList<Short>(g.indices.size)
            var t = 0
            while (t + 2 < g.indices.size) {
                val a = g.indices[t].toInt() and 0xFFFF
                val b = g.indices[t + 1].toInt() and 0xFFFF
                val c = g.indices[t + 2].toInt() and 0xFFFF
                val cx = (g.verts[a * VERT_FLOATS] + g.verts[b * VERT_FLOATS] + g.verts[c * VERT_FLOATS]) / 3f
                val cz = (g.verts[a * VERT_FLOATS + 2] + g.verts[b * VERT_FLOATS + 2] +
                    g.verts[c * VERT_FLOATS + 2]) / 3f
                // 必须同时落在尾门的 x 跨度**和** z 跨度内。
                // 只看 x 会把 LampDrl 那种从车头贯穿到车尾的灯（z −2.27~2.27）的前灯部分
                // 也拆到尾门上，前脸灯就会跟着尾门飞起来。
                if (cx >= xMin && cx <= xMax && cz >= zMin && cz <= zMax) {
                    inner.add(g.indices[t]); inner.add(g.indices[t + 1]); inner.add(g.indices[t + 2])
                } else {
                    outer.add(g.indices[t]); outer.add(g.indices[t + 1]); outer.add(g.indices[t + 2])
                }
                t += 3
            }
            // 整条都落在某一侧就不拆，避免无意义地增加 draw call
            if (inner.isEmpty() || outer.isEmpty()) continue

            val mid = CarData(
                g.name + "#trunk", g.verts, inner.toShortArray(),
                6, hinge.hingeAxis, hinge.angleDeg, g.lampRole, hinge.pivot,
            )
            val rest = CarData(
                g.name, g.verts, outer.toShortArray(),
                0, g.hingeAxis, g.angleDeg, g.lampRole, g.pivot,
            )
            groups[groups.indexOf(g)] = rest
            added.add(mid)
            android.util.Log.i(
                "CarAssets",
                "尾灯带拆分 ${g.name}: 中间 ${inner.size / 3} 片 -> 尾门, 两侧 ${outer.size / 3} 片留车身",
            )
        }
        groups.addAll(added)
    }

    fun load(ctx: android.content.Context): List<CarData> = runCatching {
        val raw = ctx.assets.open("car.bin").use { it.readBytes() }
        if (raw.size < 12 || String(raw, 0, 4) != "CARM") return emptyList()
        val bb = ByteBuffer.wrap(raw).order(ByteOrder.LITTLE_ENDIAN)
        val version = bb.getInt(4)
        val groups = bb.getInt(8)
        require(version in 1..6) { "Unsupported CARM version $version" }
        require(groups in 1..4096) { "Invalid CARM group count $groups" }
        var off = 12
        fun requireBytes(length: Long) {
            require(length >= 0 && off.toLong() + length <= raw.size.toLong()) {
                "Truncated CARM v$version at $off: need $length bytes"
            }
        }
        val names = ArrayList<String>(groups)
        if (version >= 3) {
            repeat(groups) {
                requireBytes(2)
                val len = bb.getShort(off).toInt() and 0xFFFF
                off += 2
                requireBytes(len.toLong())
                names.add(String(raw, off, len, Charsets.UTF_8))
                off += len
            }
        }
        val out = ArrayList<CarData>(groups)
        repeat(groups) { gi ->
            requireBytes(if (version >= 6) 72 else if (version >= 4) 36 else 20)
            val _matId = bb.getInt(off)
            val vcount = bb.getInt(off + 4)
            val icount = bb.getInt(off + 8)
            val idx32 = bb.getInt(off + 12)
            require(vcount in 1..65536 && icount > 0 && icount % 3 == 0 && idx32 in 0..1) {
                "Invalid geometry counts in CARM group $gi"
            }
            if (version == 6) require(vcount <= 65535 && idx32 == 0) {
                "CARM v6 group $gi must use split 16-bit indices"
            }
            // v4 组头 36B：多出 kind/axis/angle/lampRole + 铰链点；v3 只有 20B
            var rigKind = 0
            var hingeAxis = 1
            var angleDeg = 0f
            var lampRole = 0
            var pivot = floatArrayOf(0f, 0f, 0f)
            // v5 使用原保留位保存位置倍率；早期 v5 和旧版资产的 0 值仍按毫米解码。
            val encodedScale = if (version >= 5) bb.getFloat(off + 32) else 0f
            val positionScale = if (encodedScale.isFinite() && encodedScale > 0f) encodedScale else 1000f
            val material = if (version == 6) {
                require(encodedScale.isFinite() && encodedScale > 0f) { "Invalid v6 position scale" }
                val rgba = FloatArray(4) { bb.getFloat(off + 36 + it * 4) }
                val spec = bb.getFloat(off + 52)
                val gloss = bb.getFloat(off + 56)
                val textureIndex = bb.getInt(off + 60)
                val flags = bb.getInt(off + 64)
                val cutoff = bb.getFloat(off + 68)
                require(rgba.all { it.isFinite() && it in 0f..1f } &&
                    spec.isFinite() && spec in 0f..8f && gloss.isFinite() && gloss in 0f..1f &&
                    textureIndex >= -1 && flags in 0..7 && (flags and 6) != 6 &&
                    cutoff.isFinite() && cutoff in 0f..1f) { "Invalid v6 material in group $gi" }
                CarMaterial(rgba, spec, gloss, textureIndex, flags, cutoff)
            } else null
            if (version >= 4) {
                rigKind = bb.get(off + 16).toInt() and 0xFF
                hingeAxis = bb.get(off + 17).toInt() and 0xFF
                angleDeg = bb.get(off + 18).toFloat()
                lampRole = bb.get(off + 19).toInt() and 0xFF
                pivot = floatArrayOf(bb.getFloat(off + 20), bb.getFloat(off + 24), bb.getFloat(off + 28))
                val maxLampRole = if (version == 6) 8 else 7
                require(rigKind in 0..6 && hingeAxis in 0..1 && lampRole in 0..maxLampRole && pivot.all { it.isFinite() }) {
                    "Invalid rig in CARM group $gi"
                }
                off += if (version == 6) 72 else 36
            } else {
                off += 20
            }

            // v5 导出器已用 normal matrix 正确变换原模型的平滑法线。
            // 保留它们才能维持曲面跨拆分顶点的连续光照；旧版及缺失法线仍用几何兜底。
            requireBytes(vcount.toLong() * (if (version == 6) 17 else 9) + icount.toLong() * (if (idx32 != 0) 4 else 2))
            val pos = FloatArray(vcount * 3)
            val posShorts = ByteBuffer.wrap(raw, off, vcount * 6)
                .order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
            for (i in 0 until vcount) {
                pos[i * 3] = posShorts.get(i * 3) / positionScale
                pos[i * 3 + 1] = posShorts.get(i * 3 + 1) / positionScale
                pos[i * 3 + 2] = posShorts.get(i * 3 + 2) / positionScale
            }
            require(pos.all { it.isFinite() }) { "Non-finite position in CARM group $gi" }
            val storedNrm = ByteArray(vcount * 3)
            System.arraycopy(raw, off + vcount * 6, storedNrm, 0, vcount * 3)
            off += vcount * 9
            val uv = if (version == 6) FloatArray(vcount * 2) { bb.getFloat(off + it * 4) }
                else FloatArray(0)
            if (version == 6) {
                require(uv.all { it.isFinite() }) { "Non-finite UV in CARM group $gi" }
                off += vcount * 8
            }

            // GL_UNSIGNED_SHORT 的索引范围是 0..65535；Short 仅用于保存原始位模式。
            val idx = ShortArray(icount)
            if (idx32 != 0) {
                val src32 = ByteBuffer.wrap(raw, off, icount * 4)
                    .order(ByteOrder.LITTLE_ENDIAN).asIntBuffer()
                for (i in 0 until icount) {
                    val index = src32.get(i)
                    require(index in 0 until vcount && index <= 65535) { "Out-of-range index in CARM group $gi" }
                    idx[i] = index.toShort()
                }
                off += icount * 4
            } else {
                val src16 = ByteBuffer.wrap(raw, off, icount * 2)
                    .order(ByteOrder.LITTLE_ENDIAN).asShortBuffer()
                for (i in 0 until icount) {
                    idx[i] = src16.get(i)
                    require((idx[i].toInt() and 0xFFFF) < vcount) { "Out-of-range index in CARM group $gi" }
                }
                off += icount * 2
            }

            // ── 从几何重算面积加权平滑法线 ──
            val nrm = FloatArray(vcount * 3)
            var fi = 0
            while (fi + 2 < icount) {
                // Kotlin Short 有符号；不做掩码会跳过含 32768..65535 索引的整个三角形，
                // 使大网格的后半部分退化成朝上的默认法线，产生车尾和饰板光照斑块。
                val a = (idx[fi].toInt() and 0xFFFF) * 3
                val b = (idx[fi + 1].toInt() and 0xFFFF) * 3
                val c = (idx[fi + 2].toInt() and 0xFFFF) * 3
                if (a >= 0 && b >= 0 && c >= 0 &&
                    a + 2 < vcount * 3 && b + 2 < vcount * 3 && c + 2 < vcount * 3
                ) {
                    val ux = pos[b] - pos[a]; val uy = pos[b + 1] - pos[a + 1]; val uz = pos[b + 2] - pos[a + 2]
                    val vx = pos[c] - pos[a]; val vy = pos[c + 1] - pos[a + 1]; val vz = pos[c + 2] - pos[a + 2]
                    val nx = uy * vz - uz * vy; val ny = uz * vx - ux * vz; val nz = ux * vy - uy * vx
                    nrm[a] += nx; nrm[a + 1] += ny; nrm[a + 2] += nz
                    nrm[b] += nx; nrm[b + 1] += ny; nrm[b + 2] += nz
                    nrm[c] += nx; nrm[c + 1] += ny; nrm[c + 2] += nz
                }
                fi += 3
            }
            // 朝向校验：若绝大多数顶点的几何法线与存储法线反向，说明三角形环绕
            // 被翻转过，整体取反（防止光照内/外颠倒）。
            var agree = 0
            var disagree = 0
            val probe = kotlin.math.min(vcount, 2048)
            var si = 0
            while (si < probe) {
                val sx = storedNrm[si * 3].toInt() / 127f
                val sy = storedNrm[si * 3 + 1].toInt() / 127f
                val sz = storedNrm[si * 3 + 2].toInt() / 127f
                val gl = nrm[si * 3] * sx + nrm[si * 3 + 1] * sy + nrm[si * 3 + 2] * sz
                if (gl > 1e-5f) agree++ else if (gl < -1e-5f) disagree++
                si++
            }
            val flip = disagree > agree * 2
            val verts = FloatArray(vcount * 6)
            var vi = 0
            while (vi < vcount) {
                verts[vi * 6] = pos[vi * 3]
                verts[vi * 6 + 1] = pos[vi * 3 + 1]
                verts[vi * 6 + 2] = pos[vi * 3 + 2]
                var nx = nrm[vi * 3]; var ny = nrm[vi * 3 + 1]; var nz = nrm[vi * 3 + 2]
                val len = kotlin.math.sqrt(nx * nx + ny * ny + nz * nz)
                // 面积加权向量的单位是平方米；0.1mm 细节约为 1e-8，不能当成零法线。
                if (len > 1e-12f) { nx /= len; ny /= len; nz /= len } else { nx = 0f; ny = 1f; nz = 0f }
                if (flip) { nx = -nx; ny = -ny; nz = -nz }
                if (version >= 5) {
                    val sx = storedNrm[vi * 3].toInt() / 127f
                    val sy = storedNrm[vi * 3 + 1].toInt() / 127f
                    val sz = storedNrm[vi * 3 + 2].toInt() / 127f
                    val storedLength = kotlin.math.sqrt(sx * sx + sy * sy + sz * sz)
                    if (storedLength > 0.5f) {
                        nx = sx / storedLength; ny = sy / storedLength; nz = sz / storedLength
                    }
                }
                verts[vi * 6 + 3] = nx
                verts[vi * 6 + 4] = ny
                verts[vi * 6 + 5] = nz
                vi++
            }

            val matName = names.getOrElse(gi) { "mat$_matId" }
            out.add(CarData(matName, verts, idx, rigKind, hingeAxis, angleDeg, lampRole, pivot, uv, material))
        }
        if (version == 6) {
            requireBytes(4)
            val textureCount = bb.getInt(off); off += 4
            require(textureCount in 0..256) { "Invalid CARM texture count $textureCount" }
            val images = ArrayList<ByteArray>(textureCount)
            repeat(textureCount) {
                requireBytes(4)
                val length = bb.getInt(off); off += 4
                require(length in 1..16 * 1024 * 1024) { "Invalid CARM texture length $length" }
                requireBytes(length.toLong())
                images.add(raw.copyOfRange(off, off + length)); off += length
            }
            require(off == raw.size) { "Unexpected trailing CARM bytes" }
            val table = CarTextureTable(images)
            for (group in out) {
                require(group.material!!.textureIndex < textureCount) { "Invalid CARM texture reference" }
                group.textureTable = table
            }
            CrashLog.note("CARM v6 decoded: $groups groups, $textureCount shared textures")
        } else dedupeTriangles(out)
        // v5 已按 GLB 部件配置分组。包围盒猜测会把本来固定的灯再次划给尾门，
        // 因此仅旧版资产保留此兼容补丁；去重仍严格限定在同一 rig 内。
        if (version < 5) reassignTailBandToTrunk(out)
        out
    }.getOrElse { failure ->
        runCatching { CrashLog.record("CarAssets.load", failure) }
        emptyList()
    }
}

/**
 * 崩溃/异常记录器。
 *
 * 为什么需要它：这台设备的 `/data/log/faultlog/` 是 Permission denied，没有 root 就拿不到
 * 系统级崩溃堆栈，只能靠日志里零散的 `PERF_BROKER ... FREEZE`、`common_exception` 去猜。
 * 这里把 GL 线程的异常、以及关键节点（车模上传成功/失败）都写进文件，并同时打到 hilog
 * （tag = CpCrash，可用 `hdc shell hilog -x | grep CpCrash` 抓出来）。
 */
internal object CrashLog {
    private const val TAG = "CpCrash"
    @Volatile private var dir: java.io.File? = null
    @Volatile private var installed = false

    fun install(ctx: android.content.Context) {
        if (installed) return
        installed = true
        dir = ctx.getExternalFilesDir(null) ?: ctx.filesDir
        // 全局未捕获异常也记一份（主线程 / 协程里的崩溃）
        val prev = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { t, e ->
            record("uncaught/${t.name}", e)
            prev?.uncaughtException(t, e)
        }
        note("CrashLog installed -> $dir")
    }

    fun note(msg: String) {
        android.util.Log.i(TAG, msg)
        append("I", msg)
    }

    fun record(where: String, t: Throwable) {
        val text = buildString {
            append("=== $where ===\n")
            append(t.javaClass.name).append(": ").append(t.message).append('\n')
            append(t.stackTraceToString())
            append('\n')
            var c = t.cause
            while (c != null) {
                append("caused by ").append(c.javaClass.name).append(": ")
                    .append(c.message).append('\n')
                c = c.cause
            }
        }
        android.util.Log.e(TAG, "CRASH at $where", t)
        // 分片打，hilog 单条有长度限制
        text.lineSequence().forEach { android.util.Log.e(TAG, it) }
        append("E", text)
    }

    private fun append(level: String, msg: String) {
        val d = dir ?: return
        runCatching {
            d.mkdirs()
            val f = java.io.File(d, "cpcrash.log")
            // 超过 256 KB 就滚一次，避免无限增长
            if (f.exists() && f.length() > 256 * 1024) {
                java.io.File(d, "cpcrash.old.log").let { old ->
                    if (old.exists()) old.delete()
                    f.renameTo(old)
                }
            }
            val ts = java.text.SimpleDateFormat("MM-dd HH:mm:ss.SSS", java.util.Locale.US)
                .format(java.util.Date())
            f.appendText("$ts $level $msg\n")
        }
    }
}
