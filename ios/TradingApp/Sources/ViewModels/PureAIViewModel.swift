import Foundation
import SwiftUI

// ── Pure-AI Experiment ViewModel ───────────────────────────────────────────────
// Drives the iOS PureAI tab. Hits /pureai/* endpoints only.
// Zero overlap with Kova (DashboardViewModel) or Wheel (WheelViewModel).

@MainActor
final class PureAIViewModel: ObservableObject {

    // ── State ────────────────────────────────────────────────────────────────
    @Published var status: PureAIStatus?
    @Published var decisions: [PureAIDecision] = []
    @Published var isLoading = false
    @Published var isRunning = false
    @Published var isSavingConfig = false
    @Published var errorMessage: String?
    @Published var cycleResult: PureAICycleResult?
    @Published var showCycleSheet = false
    @Published var lastRefreshed: Date?

    // Config edit state (bound to controls in settings section)
    @Published var editEnabled: Bool = true
    @Published var editMaxBuys: Int = 3
    @Published var editInterval: Int = 60
    @Published var editMaxSearches: Int = 8
    @Published var editModel: String = "claude-opus-4-8"
    @Published var modelOptions: [String] = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]

    private let api = APIService.shared

    // ── Load ─────────────────────────────────────────────────────────────────
    func loadStatus() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            status = try await api.fetch("/pureai/status")
            if let s = status?.settings {
                editEnabled     = s.enabled
                editMaxBuys     = s.maxBuysPerCycle
                editInterval    = s.cycleIntervalMinutes
                editMaxSearches = s.maxSearchesPerCycle
                editModel       = s.model
                if let opts = s.modelOptions, !opts.isEmpty { modelOptions = opts }
            }
            lastRefreshed = Date()
        } catch {
            errorMessage = "Failed to load: \(error.localizedDescription)"
        }
    }

    func loadDecisions() async {
        do {
            decisions = try await api.fetch("/pureai/decisions?limit=20")
        } catch {
            // non-fatal; decisions tab just stays empty
        }
    }

    // ── Run one cycle manually ────────────────────────────────────────────────
    func runCycle() async {
        isRunning = true
        defer { isRunning = false }
        do {
            cycleResult = try await api.fetch("/pureai/run", method: "POST", timeout: 120)
            showCycleSheet = true
            await loadStatus()
            await loadDecisions()
        } catch {
            errorMessage = "Cycle failed: \(error.localizedDescription)"
        }
    }

    // ── Save config ───────────────────────────────────────────────────────────
    func saveConfig() async {
        isSavingConfig = true
        defer { isSavingConfig = false }
        do {
            struct Req: Encodable {
                let enabled: Bool
                let max_buys_per_cycle: Int
                let cycle_interval_minutes: Int
                let max_searches_per_cycle: Int
                let model: String
            }
            let body = Req(
                enabled: editEnabled,
                max_buys_per_cycle: editMaxBuys,
                cycle_interval_minutes: editInterval,
                max_searches_per_cycle: editMaxSearches,
                model: editModel
            )
            struct Resp: Decodable { let message: String }
            let _: Resp = try await api.fetch("/pureai/config", method: "POST",
                                              body: body, timeout: 15)
            await loadStatus()
        } catch {
            errorMessage = "Config save failed: \(error.localizedDescription)"
        }
    }

    // ── Display helpers ───────────────────────────────────────────────────────
    var equityDisplay: String {
        guard let eq = status?.equity, eq > 0 else { return "--" }
        let fmt = NumberFormatter()
        fmt.numberStyle = .decimal
        fmt.maximumFractionDigits = 0
        return "$" + (fmt.string(from: NSNumber(value: eq)) ?? String(Int(eq)))
    }

    var realizedPlDisplay: String {
        guard let pl = status?.realizedPl else { return "--" }
        return String(format: "%@$%.2f", pl >= 0 ? "+" : "", pl)
    }

    var realizedPlPositive: Bool { (status?.realizedPl ?? 0) >= 0 }

    var holdingsCount: Int { status?.holdings?.count ?? 0 }

    var modelDisplay: String {
        let m = editModel.isEmpty ? (status?.model ?? "claude-opus-4-8") : editModel
        return m.replacingOccurrences(of: "claude-", with: "")
                .replacingOccurrences(of: "-", with: " ")
                .capitalized
    }

    var isPaper: Bool { status?.paper ?? true }

    func formattedTimestamp(_ raw: String) -> String {
        let fmts = [
            "yyyy-MM-dd'T'HH:mm:ss.SSSSSSZZZZZ",
            "yyyy-MM-dd'T'HH:mm:ssZZZZZ",
            "yyyy-MM-dd HH:mm:ss.SSSSSSZZZZZ",
            "yyyy-MM-dd HH:mm:ssZZZZZ",
        ]
        let out = DateFormatter()
        out.dateStyle = .short; out.timeStyle = .short
        for fmt in fmts {
            let df = DateFormatter(); df.dateFormat = fmt
            if let d = df.date(from: raw) { return out.string(from: d) }
        }
        return raw
    }
}
