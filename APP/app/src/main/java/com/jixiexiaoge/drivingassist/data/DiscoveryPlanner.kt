package com.jixiexiaoge.drivingassist.data

/**
 * 局域网发现的**纯计算**部分：地址校验、扫描网段推导、扫描节奏、设备优先级。
 *
 * 为什么单独抽出来：
 * - 真机（尤其卓易通这类 Android 容器）里网卡枚举和 UDP 广播行为没法在单测里复现，
 *   把决策逻辑做成无副作用函数，才能在 `testDebugUnitTest` 里覆盖；
 * - 8.3.2 之前 `discoveryHosts()` 只按容器内网卡算 `/24`，容器里拿到的常常是 172.x
 *   的 NAT 地址甚至空列表，于是永远扫不到车机的 192.168.123.0/24。现在改成
 *   「上次设备 IP → 默认网关 → 本机网卡」多来源并集，第一个线索就够救回来。
 */
object DiscoveryPlanner {

    /** 只接受可以被当作「局域网单播地址」的 IPv4：1.0.0.0 – 223.255.255.255，排除回环。 */
    fun sanitizeIpv4(raw: String?): String? {
        val text = raw?.trim().orEmpty()
        val octets = text.split(".")
        if (octets.size != 4) return null
        val values = octets.map { part ->
            if (part.isEmpty() || part.any { it !in '0'..'9' }) return null
            part.toIntOrNull() ?: return null
        }
        if (values.any { it !in 0..255 }) return null
        if (values[0] !in 1..223) return null
        if (values[0] == 127) return null
        return values.joinToString(".")
    }

    /** `192.168.123.70` → `192.168.123`；非法地址返回 null。 */
    fun subnetOf(ip: String?): String? = sanitizeIpv4(ip)?.substringBeforeLast('.')

    /**
     * 把若干地址线索折算成要扫描的 /24 前缀，按传入顺序去重（顺序即优先级）。
     *
     * @param clues 上次连接的设备 IP、默认网关、本机各网卡地址混在一起传进来即可
     * @param max 最多扫几段 /24，防止极端情况下把扫描拖长
     */
    fun scanPrefixes(clues: List<String>, max: Int = 4): List<String> {
        if (max <= 0) return emptyList()
        val result = LinkedHashSet<String>()
        for (clue in clues) {
            val prefix = subnetOf(clue) ?: continue
            result.add(prefix)
            if (result.size >= max) break
        }
        return result.toList()
    }

    /**
     * 生成待探测主机列表：每个前缀的 1..254，排除本机地址。
     * 网络地址（.0）与广播地址（.255）天然不落在 1..254 内。
     */
    fun scanHosts(
        prefixes: List<String>,
        selfIps: Collection<String>,
        lastOctet: Int = 254,
    ): List<String> {
        if (prefixes.isEmpty() || lastOctet <= 0) return emptyList()
        val self = selfIps.mapNotNull { sanitizeIpv4(it) }.toSet()
        val hosts = ArrayList<String>(prefixes.size * lastOctet)
        for (prefix in prefixes) {
            for (octet in 1..lastOctet.coerceAtMost(254)) {
                val ip = "$prefix.$octet"
                if (ip !in self) hosts.add(ip)
            }
        }
        return hosts
    }

    /**
     * 扫描间隔：已连接时不需要扫（返回 [capMs]，外层会拿它当心跳间隔）；
     * 未连接时从 [baseMs] 起，连续未命中按 2 的幂退避，最多 [capMs]。
     */
    fun scanDelayMs(
        misses: Int,
        connected: Boolean,
        baseMs: Long = 5_000L,
        capMs: Long = 20_000L,
    ): Long {
        // 退避不能太狠：车端 shim/台架重启是很常见的事（升级、改配置、断电重来），
        // 原来 base=15s、cap=60s，叠加 isLinkHealthy() 的 12s 宽限后最坏要等 72s
        // 才重扫一次 —— 体感就是"永远连不上，必须清数据"。
        // 局域网扫描一次约 3s，5s~20s 的退避既不会打爆网络，也能在 30s 内自愈。
        if (connected) return capMs
        val shift = misses.coerceIn(0, 3)
        val scaled = baseMs shl shift
        return scaled.coerceIn(baseMs, capMs)
    }

    /** 一台候选设备的发现事实，用于选主。 */
    data class Candidate(
        val ip: String,
        val port: Int,
        val verified: Boolean,
        val responseMs: Long,
        val fromBroadcast: Boolean,
        val lastSeenMs: Long = 0L,
    )

    /**
     * 自动连接优先级：
     * 1. 上次成功连接的 IP 还在列表里 → 直接用它（冷启动恢复最可靠）；
     * 2. 已验证且 HTTP 响应最快的那台；
     * 3. 广播发现的优先于扫描发现的；
     * 4. 只剩一台时直接用它。
     */
    fun pickBest(candidates: List<Candidate>, lastIp: String?): Candidate? {
        if (candidates.isEmpty()) return null
        val sanitizedLast = sanitizeIpv4(lastIp)
        if (sanitizedLast != null) {
            candidates.firstOrNull { it.ip == sanitizedLast }?.let { return it }
        }
        val verified = candidates.filter { it.verified }
        if (verified.isNotEmpty()) {
            return verified.sortedWith(
                compareByDescending<Candidate> { it.fromBroadcast }.thenBy { it.responseMs },
            ).first()
        }
        if (candidates.size == 1) return candidates.first()
        return candidates.sortedWith(
            compareByDescending<Candidate> { it.fromBroadcast }.thenByDescending { it.lastSeenMs },
        ).first()
    }
}
