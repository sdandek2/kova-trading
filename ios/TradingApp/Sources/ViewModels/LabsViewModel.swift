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

// ── SEC Intel models ──────────────────────────────────────────────────────────

struct SecIntelStatus: Decodable {
    let configured: Bool
    let paperMode: Bool
    let openPositions: Int
    let maxPositions: Int
    let signalCount180d: Int
    let threadAlive: Bool

    enum CodingKeys: String, CodingKey {
        case configured
        case paperMode = "paper_mode"
        case openPositions = "open_positions"
        case maxPositions = "max_positions"
        case signalCount180d = "signal_count_180d"
        case threadAlive = "thread_alive"
    }
}

struct SecIntelSignal: Decodable, Identifiable {
    var id: String { "\(institution)-\(ticker)-\(quarter)" }
    let institution: String
    let ticker: String
    let action: String
    let quarter: String
    let filedDate: String
    let score: Int
    let isWhitelist: Bool
    let holdDays: Int

    enum CodingKeys: String, CodingKey {
        case institution, ticker, action, quarter, score
        case filedDate = "filed_date"
        case isWhitelist = "is_whitelist"
        case holdDays = "hold_days"
    }
}

struct SecIntelPosition: Decodable, Identifiable {
    let id: Int
    let ticker: String
    let institution: String?
    let quarter: String?
    let entryPrice: Double?
    let shares: Int?
    let sizeUsd: Double?
    let stop: Double?
    let peak: Double?
    let l1Exit: Double?
    let l2Exit: Double?
    let trailStop: Double?
    let entryDate: String?
    let maxHold: String?
    let status: String?

    enum CodingKeys: String, CodingKey {
        case id = "id"
        case ticker, institution, quarter, stop, peak, status
        case entryPrice = "entry_price"
        case shares
        case sizeUsd = "size_usd"
        case l1Exit = "l1_exit"
        case l2Exit = "l2_exit"
        case trailStop = "trail_stop"
        case entryDate = "entry_date"
        case maxHold = "max_hold"
    }
}

struct SecIntelTrade: Decodable, Identifiable {
    let id: Int
    let ticker: String
    let institution: String?
    let quarter: String?
    let entry: Double?
    let exit: Double?
    let shares: Int?
    let pl: Double?
    let plPct: Double?
    let reason: String?
    let entryDate: String?
    let exitDate: String?
    let holdDays: Int?

    enum CodingKeys: String, CodingKey {
        case id, ticker, institution, quarter, entry, exit, shares, reason
        case plPct = "pl_pct"
        case entryDate = "entry_date"
        case exitDate = "exit_date"
        case holdDays = "hold_days"
    }
}

struct SecIntelPerformance: Decodable {
    let totalTrades: Int?
    let wins: Int?
    let losses: Int?
    let winRate: Double?
    let netPl: Double?
    let avgPlPct: Double?
    let bestTrade: Double?
    let worstTrade: Double?
    let avgHoldDays: Double?
    let message: String?

    enum CodingKeys: String, CodingKey {
        case totalTrades = "total_trades"
        case wins, losses, message
        case winRate = "win_rate"
        case netPl = "net_pl"
        case avgPlPct = "avg_pl_pct"
        case bestTrade = "best_trade"
        case worstTrade = "worst_trade"
        case avgHoldDays = "avg_hold_days"
    }
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

// Result body is not used — empty struct so polymorphic/unknown fields never cause decode failures
struct EngineRunResult: Decodable {}

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
        case secIntel  = "sec-intel"

        var displayName: String {
            switch self {
            case .squeeze:   return "Squeeze"
            case .spillover: return "Spillover"
            case .revision:  return "Revision"
            case .secIntel:  return "SEC Intel"
            }
        }

        var icon: String {
            switch self {
            case .squeeze:   return "flame.fill"
            case .spillover: return "arrow.triangle.branch"
            case .revision:  return "chart.line.uptrend.xyaxis"
            case .secIntel:  return "building.columns.fill"
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

    // SEC Intel state
    @Published var secIntelStatus: SecIntelStatus?
    @Published var secIntelSignals: [SecIntelSignal] = []
    @Published var secIntelPositions: [SecIntelPosition] = []
    @Published var secIntelHistory: [SecIntelTrade] = []
    @Published var secIntelPerformance: SecIntelPerformance?

    private let api = APIService.shared
    private var refreshTask: Task<Void, Never>?

    // ── Load ─────────────────────────────────────────────────────────────────

    func loadAll() async {
        isLoading = true
        defer { isLoading = false }
        if selectedEngine == .secIntel {
            await loadSecIntel()
        } else {
            async let statusTask: AllEngineStatus? = try? api.fetch("/experiments/status")
            async let posTask: [ExperimentPosition]? = try? api.fetch("/experiments/\(selectedEngine.rawValue)/positions")
            async let sumTask: EngineSummary? = try? api.fetch("/experiments/\(selectedEngine.rawValue)/summary")
            let (s, p, sm) = await (statusTask, posTask, sumTask)
            if let s { allStatus = s }
            if let p { positions = p }
            if let sm { summary = sm }
        }
        lastRefreshed = Date()
    }

    func loadPositions() async {
        if selectedEngine == .secIntel {
            await loadSecIntel()
            return
        }
        async let posTask: [ExperimentPosition]? = try? api.fetch("/experiments/\(selectedEngine.rawValue)/positions")
        async let sumTask: EngineSummary? = try? api.fetch("/experiments/\(selectedEngine.rawValue)/summary")
        let (p, sm) = await (posTask, sumTask)
        if let p { positions = p }
        if let sm { summary = sm }
    }

    private func loadSecIntel() async {
        struct SignalWrapper: Decodable { let signals: [SecIntelSignal] }
        struct PositionWrapper: Decodable { let positions: [SecIntelPosition] }
        struct TradeWrapper: Decodable { let trades: [SecIntelTrade] }

        async let st: SecIntelStatus?          = try? api.fetch("/sec-intel/status")
        async let sw: SignalWrapper?            = try? api.fetch("/sec-intel/signals?limit=100")
        async let pw: PositionWrapper?          = try? api.fetch("/sec-intel/positions")
        async let tw: TradeWrapper?             = try? api.fetch("/sec-intel/history?limit=20")
        async let perf: SecIntelPerformance?    = try? api.fetch("/sec-intel/performance")

        let (status, signals, positions, trades, performance) = await (st, sw, pw, tw, perf)
        if let v = status     { secIntelStatus = v }
        if let v = signals    { secIntelSignals = v.signals }
        if let v = positions  { secIntelPositions = v.positions }
        if let v = trades     { secIntelHistory = v.trades }
        if let v = performance { secIntelPerformance = v }
    }

    func switchEngine(_ engine: Engine) async {
        selectedEngine = engine
        await loadPositions()
    }

    func triggerScan() async {
        isRunning = true
        defer { isRunning = false }
        if selectedEngine == .secIntel {
            let _: EngineRunResult? = try? await api.fetch("/sec-intel/process-signals", method: "POST")
            await loadAll()
            return
        }
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
        case .secIntel:  return nil  // SEC Intel has its own status card
        }
    }

    var openPositions: [ExperimentPosition] {
        positions.filter { $0.status == "open" }
    }

    var closedPositions: [ExperimentPosition] {
        positions.filter { $0.status == "closed" || $0.status == "stopped" }
    }
}
