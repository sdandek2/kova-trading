import Foundation
import SwiftUI

// ── Models ────────────────────────────────────────────────────────────────────

struct EngineStatus: Decodable {
    let engine: String?
    let configured: Bool
    let running: Bool
    let equity: Double?
    let cash: Double?
    let openPositions: Int?
    let closedTrades: Int?
    let realizedPl: Double?
    let winRate: Double?
    let accountError: String?

    enum CodingKeys: String, CodingKey {
        case engine, configured, running, equity, cash
        case openPositions = "open_positions"
        case closedTrades = "closed_trades"
        case realizedPl = "realized_pl"
        case winRate = "win_rate"
        case accountError = "account_error"
    }
}

struct AllEngineStatus: Decodable {
    let squeeze: EngineStatus
    let spillover: EngineStatus
    let revision: EngineStatus
}

struct ExperimentPosition: Decodable, Identifiable {
    let id: Int
    let symbol: String
    let entryPrice: Double?
    let entryDate: String?
    let shares: Int?
    let stopPrice: Double?
    let targetPrice: Double?
    let status: String?
    let exitPrice: Double?
    let exitDate: String?
    let realizedPl: Double?
    // squeeze-specific
    let daysToCover: Double?
    let volumeRatio: Double?
    // spillover-specific
    let triggerSymbol: String?
    let triggerBeatPct: Double?
    let sector: String?
    // revision-specific
    let beatPct: Double?
    let notes: String?

    enum CodingKeys: String, CodingKey {
        case id, symbol, shares, status, sector, notes
        case entryPrice = "entry_price"
        case entryDate = "entry_date"
        case stopPrice = "stop_price"
        case targetPrice = "target_price"
        case exitPrice = "exit_price"
        case exitDate = "exit_date"
        case realizedPl = "realized_pl"
        case daysToCover = "days_to_cover"
        case volumeRatio = "volume_ratio"
        case triggerSymbol = "trigger_symbol"
        case triggerBeatPct = "trigger_beat_pct"
        case beatPct = "beat_pct"
    }
}

struct EngineRunResult: Decodable {
    let engine: String?
    let result: RunResultDetail?

    struct RunResultDetail: Decodable {
        let skipped: String?
        let bought: [BoughtItem]?
        let candidates: [String]?

        struct BoughtItem: Decodable {
            let symbol: String?
        }
    }
}

struct CloseResult: Decodable {
    let closed: Bool?
    let symbol: String?
    let exitPrice: Double?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case closed, symbol, error
        case exitPrice = "exit_price"
    }
}

struct EngineSummary: Decodable {
    let engine: String?
    let totalTrades: Int?
    let netPl: Double?
    let winRate: Double?
    let bestTrade: Double?
    let worstTrade: Double?

    enum CodingKeys: String, CodingKey {
        case engine
        case totalTrades = "total_trades"
        case netPl = "net_pl"
        case winRate = "win_rate"
        case bestTrade = "best_trade"
        case worstTrade = "worst_trade"
    }
}

// ── ViewModel ─────────────────────────────────────────────────────────────────

@MainActor
final class LabsViewModel: ObservableObject {

    enum Engine: String, CaseIterable {
        case squeeze   = "squeeze"
        case spillover = "spillover"
        case revision  = "revision"

        var displayName: String {
            switch self {
            case .squeeze:   return "Squeeze"
            case .spillover: return "Spillover"
            case .revision:  return "Revision"
            }
        }

        var icon: String {
            switch self {
            case .squeeze:   return "flame.fill"
            case .spillover: return "arrow.triangle.branch"
            case .revision:  return "chart.line.uptrend.xyaxis"
            }
        }
    }

    @Published var selectedEngine: Engine = .squeeze
    @Published var allStatus: AllEngineStatus?
    @Published var positions: [ExperimentPosition] = []
    @Published var summary: EngineSummary?
    @Published var isLoading = false
    @Published var isRunning = false
    @Published var errorMessage: String?
    @Published var lastRefreshed: Date?

    private let api = APIService.shared
    private var refreshTask: Task<Void, Never>?

    // ── Load ─────────────────────────────────────────────────────────────────

    func loadAll() async {
        isLoading = true
        defer { isLoading = false }
        async let statusTask: AllEngineStatus? = try? api.fetch("/experiments/status")
        async let posTask: [ExperimentPosition]? = try? api.fetch("/experiments/\(selectedEngine.rawValue)/positions")
        async let sumTask: EngineSummary? = try? api.fetch("/experiments/\(selectedEngine.rawValue)/summary")
        let (s, p, sm) = await (statusTask, posTask, sumTask)
        if let s { allStatus = s }
        if let p { positions = p }
        if let sm { summary = sm }
        lastRefreshed = Date()
    }

    func loadPositions() async {
        do {
            positions = try await api.fetch("/experiments/\(selectedEngine.rawValue)/positions")
            summary   = try await api.fetch("/experiments/\(selectedEngine.rawValue)/summary")
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func switchEngine(_ engine: Engine) async {
        selectedEngine = engine
        await loadPositions()
    }

    func triggerScan() async {
        isRunning = true
        defer { isRunning = false }
        do {
            let _: EngineRunResult = try await api.fetch(
                "/experiments/\(selectedEngine.rawValue)/run", method: "POST"
            )
            await loadAll()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func closePosition(_ posId: Int) async {
        do {
            let _: CloseResult = try await api.fetch(
                "/experiments/\(selectedEngine.rawValue)/close/\(posId)", method: "POST"
            )
            await loadPositions()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    // ── Auto-refresh ──────────────────────────────────────────────────────────

    func startAutoRefresh() {
        stopAutoRefresh()
        refreshTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(60))
                if !Task.isCancelled {
                    await loadAll()
                }
            }
        }
    }

    func stopAutoRefresh() {
        refreshTask?.cancel()
        refreshTask = nil
    }

    // ── Convenience getters ───────────────────────────────────────────────────

    var currentStatus: EngineStatus? {
        guard let s = allStatus else { return nil }
        switch selectedEngine {
        case .squeeze:   return s.squeeze
        case .spillover: return s.spillover
        case .revision:  return s.revision
        }
    }

    var openPositions: [ExperimentPosition] {
        positions.filter { $0.status == "open" }
    }

    var closedPositions: [ExperimentPosition] {
        positions.filter { $0.status == "closed" || $0.status == "stopped" }
    }
}
