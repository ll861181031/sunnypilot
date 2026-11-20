package com.jixiexiaoge.drivingassist.data

import com.google.common.truth.Truth.assertThat
import org.junit.Test

/**
 * 局域网发现的纯逻辑单测。
 *
 * 覆盖 8.3.2 的三个真实故障点：
 * 1. 广播里的 `ip` 字段可能缺失/非法，必须回退 UDP 源地址；
 * 2. 容器里按内网卡算 /24 会扫错网段，必须支持「上次设备 → 网关 → 网卡」并集；
 * 3. 扫描要有退避、要到点就停、同 IP 断开要能被重连优先级识别。
 */
class DiscoveryPlannerTest {

    @Test
    fun `sanitize accepts only unicast ipv4 and rejects loopback or malformed`() {
        assertThat(DiscoveryPlanner.sanitizeIpv4("192.168.123.70")).isEqualTo("192.168.123.70")
        assertThat(DiscoveryPlanner.sanitizeIpv4(" 10.0.0.5 ")).isEqualTo("10.0.0.5")

        assertThat(DiscoveryPlanner.sanitizeIpv4(null)).isNull()
        assertThat(DiscoveryPlanner.sanitizeIpv4("")).isNull()
        assertThat(DiscoveryPlanner.sanitizeIpv4("192.168.123")).isNull()
        assertThat(DiscoveryPlanner.sanitizeIpv4("192.168.123.256")).isNull()
        assertThat(DiscoveryPlanner.sanitizeIpv4("0.0.0.0")).isNull()
        assertThat(DiscoveryPlanner.sanitizeIpv4("127.0.0.1")).isNull()
        assertThat(DiscoveryPlanner.sanitizeIpv4("abc.def.1.1")).isNull()
        assertThat(DiscoveryPlanner.sanitizeIpv4("192.168.1.-1")).isNull()
    }

    @Test
    fun `subnetOf keeps the first three octets`() {
        assertThat(DiscoveryPlanner.subnetOf("192.168.123.70")).isEqualTo("192.168.123")
        assertThat(DiscoveryPlanner.subnetOf("不合法")).isNull()
    }

    @Test
    fun `scan prefixes put the last known device first and de-duplicate`() {
        val prefixes = DiscoveryPlanner.scanPrefixes(
            listOf("192.168.123.70", "172.17.0.1", "192.168.123.134", "bad-value"),
        )
        assertThat(prefixes).containsExactly("192.168.123", "172.17.0").inOrder()
    }

    @Test
    fun `scan prefixes honour the cap so a bad clue list cannot explode the scan`() {
        val clues = (1..10).map { "10.0.$it.1" }
        assertThat(DiscoveryPlanner.scanPrefixes(clues, max = 2)).hasSize(2)
        assertThat(DiscoveryPlanner.scanPrefixes(clues, max = 0)).isEmpty()
    }

    @Test
    fun `scan hosts cover one slash24 and skip our own address`() {
        val hosts = DiscoveryPlanner.scanHosts(listOf("192.168.123"), selfIps = listOf("192.168.123.134"))
        assertThat(hosts).hasSize(253)
        assertThat(hosts).contains("192.168.123.70")
        assertThat(hosts).doesNotContain("192.168.123.134")
        assertThat(hosts).doesNotContain("192.168.123.0")
        assertThat(hosts).doesNotContain("192.168.123.255")
    }

    @Test
    fun `scan hosts of several prefixes stay ordered by prefix`() {
        val hosts = DiscoveryPlanner.scanHosts(
            listOf("192.168.123", "172.17.0"), selfIps = emptyList(), lastOctet = 2,
        )
        assertThat(hosts).containsExactly(
            "192.168.123.1", "192.168.123.2", "172.17.0.1", "172.17.0.2",
        ).inOrder()
    }

    @Test
    fun `scan delay backs off and is capped`() {
        assertThat(DiscoveryPlanner.scanDelayMs(0, connected = false)).isEqualTo(5_000L)
        assertThat(DiscoveryPlanner.scanDelayMs(1, connected = false)).isEqualTo(10_000L)
        assertThat(DiscoveryPlanner.scanDelayMs(2, connected = false)).isEqualTo(20_000L)
        assertThat(DiscoveryPlanner.scanDelayMs(9, connected = false)).isEqualTo(20_000L)
        // 已连接时返回上限，外层拿它当心跳间隔用
        assertThat(DiscoveryPlanner.scanDelayMs(0, connected = true)).isEqualTo(20_000L)
    }

    private fun candidate(
        ip: String,
        verified: Boolean = true,
        responseMs: Long = 50,
        fromBroadcast: Boolean = false,
        lastSeenMs: Long = 0,
    ) = DiscoveryPlanner.Candidate(
        ip = ip,
        port = 7000,
        verified = verified,
        responseMs = responseMs,
        fromBroadcast = fromBroadcast,
        lastSeenMs = lastSeenMs,
    )

    @Test
    fun `last known device wins over a faster newcomer`() {
        val best = DiscoveryPlanner.pickBest(
            listOf(candidate("192.168.123.80", responseMs = 5), candidate("192.168.123.70", responseMs = 90)),
            lastIp = "192.168.123.70",
        )
        assertThat(best?.ip).isEqualTo("192.168.123.70")
    }

    @Test
    fun `verified fastest device wins without a remembered ip`() {
        val best = DiscoveryPlanner.pickBest(
            listOf(
                candidate("192.168.123.80", verified = false, responseMs = 5),
                candidate("192.168.123.70", responseMs = 120),
                candidate("192.168.123.90", responseMs = 40),
            ),
            lastIp = null,
        )
        assertThat(best?.ip).isEqualTo("192.168.123.90")
    }

    @Test
    fun `broadcast source beats a scan hit when response times tie`() {
        val best = DiscoveryPlanner.pickBest(
            listOf(
                candidate("192.168.123.90", responseMs = 60, fromBroadcast = false),
                candidate("192.168.123.70", responseMs = 60, fromBroadcast = true),
            ),
            lastIp = null,
        )
        assertThat(best?.ip).isEqualTo("192.168.123.70")
    }

    @Test
    fun `no candidates means no connection attempt`() {
        assertThat(DiscoveryPlanner.pickBest(emptyList(), lastIp = "192.168.123.70")).isNull()
    }
}
