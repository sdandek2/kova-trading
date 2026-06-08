import Foundation
import SwiftUI

// ── Wheel Bot ViewModel ────────────────────────────────────────────────────────
// Drives the iOS Wheel tab. Completely separate from Kova's DashboardViewModel.
// All API calls hit /wheel/* endpoints — zero overlap with Kova routes.

@MainActor
final class WheelViewModel: ObservableObject {

    // ── State ────────────────────────────────────────────────────────────────
    @Published var status: WheelStatus?
    @Published var opportunities: [WheelOpportunity] = []
    @Published var isLoading = false
    @Published var isRefreshingUniverse = false
    @Published var isRunningCycle = false
    @Published var isScanningOpps = false
    @Published var errorMessage: String?
    @Published var lastRefreshedAt: Date?
    @Published var cycleResult: String?
    @Published var showCycleResult = false

    private let api = APIService.shared

    // ── Load Dashboard ────────────────────────────────────────────────────────
    func loadStatus() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            status = try await api.fetch("/wheel/status")
            lastRefreshedAt = Date()
        } catch {
            errorMessage = "Failed to load wheel status: \(error.localizedDescription)"
        }
    }

    // ── Scan Opportunities (preview only, no orders) ──────────────────────────
    func scanOpportunities() async {
        isScanningOpps = true
        defer { isScanningOpps = false }
        do {
            struct ScanResponse: Decodable {
                let opportunities: [WheelOpportunity]
            }
            let resp: ScanResponse = try await api.fetch("/wheel/scan")
            opportunities = resp.opportunities
        } catch {
            errorMessage = "Scan failed: \(error.localizedDescription)"
        }
    }

    // ── Run Wheel Cycle (full: check + trade) ─────────────────────────────────
    func runCycle() async {
        isRunningCycle = true
        cycleResult = nil
        defer { isRunningCycle = false }
        do {
            struct CycleResponse: Decodable {
                let status: String
                let message: String
            }
            let resp: CycleResponse = try await api.fetch("/wheel/execute", method: "POST")
            cycleResult = resp.message
            showCycleResult = true
            // Reload status after cycle
            await loadStatus()
        } catch {
            errorMessage = "Cycle failed: \(error.localizedDescription)"
        }
    }

    // ── Refresh Universe (AI re-discovery) ───────────────────────────────────
    func refreshUniverse() async {
        isRefreshingUniverse = true
        defer { isRefreshingUniverse = false }
        do {
            struct UniverseRefreshResponse: Decodable {
                let status: String
                let count: Int
                let message: String
            }
            let resp: UniverseRefreshResponse = try await api.fetch(
                "/wheel/universe/refresh",
                method: "POST"
            )
            cycleResult = resp.message
            showCycleResult = true
            await loadStatus()
        } catch {
            errorMessage = "Universe refresh failed: \(error.localizedDescription)"
        }
    }

    // ── Helpers ───────────────────────────────────────────────────────────────
    var totalPremiumDisplay: String {
        let total = status?.summary.totalPremiumCollected ?? 0
        return String(format: "$%.0f", total)
    }

    var realizedPlDisplay: String {
        let pl = status?.summary.totalRealizedPl ?? 0
        return String(format: "%@$%.0f", pl >= 0 ? "+" : "", pl)
    }

    var isLive: Bool {
        status?.mode == "live"
    }

    var slotsUsed: Int { status?.activeCount ?? 0 }
    var slotsTotal: Int { status?.maxPositions ?? 5 }
    var slotsDisplay: String { "\(slotsUsed) / \(slotsTotal)" }
}
