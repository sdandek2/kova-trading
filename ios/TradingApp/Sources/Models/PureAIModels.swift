import Foundation

// ── Pure-AI Experiment Models ──────────────────────────────────────────────────
// Third Alpaca paper account: AI searches the web and decides alone.
// 30-day ablation test vs Kova (pipeline+AI). Zero overlap with Kova/Wheel types.

// MARK: - Settings

struct PureAISettings: Codable {
    var enabled: Bool
    var maxBuysPerCycle: Int
    var cycleIntervalMinutes: Int
    var maxPositionPct: Double
    var maxSearchesPerCycle: Int
    var model: String
    var modelOptions: [String]?
    var profitReservePct: Double?

    enum CodingKeys: String, CodingKey {
        case enabled, model
        case maxBuysPerCycle      = "max_buys_per_cycle"
        case cycleIntervalMinutes = "cycle_interval_minutes"
        case maxPositionPct       = "max_position_pct"
        case maxSearchesPerCycle  = "max_searches_per_cycle"
        case modelOptions         = "model_options"
        case profitReservePct     = "profit_reserve_pct"
    }
}

// MARK: - Live Holdings

struct PureAIHolding: Identifiable, Decodable {
    let id = UUID()
    let symbol: String
    let qty: Double
    let entryPrice: Double
    let currentPrice: Double
    let unrealizedPl: Double
    let unrealizedPlPct: Double

    enum CodingKeys: String, CodingKey {
        case symbol, qty
        case entryPrice      = "entry_price"
        case currentPrice    = "current_price"
        case unrealizedPl    = "unrealized_pl"
        case unrealizedPlPct = "unrealized_pl_pct"
        // `id` intentionally omitted — default UUID() used
    }

    var isUp: Bool { unrealizedPl >= 0 }
}

// MARK: - Status (full dashboard)

struct PureAIStatus: Decodable {
    let configured: Bool
    let paper: Bool
    let model: String
    let settings: PureAISettings
    let equity: Double?
    let cash: Double?
    let holdings: [PureAIHolding]?
    let accountError: String?
    let closedTrades: Int?
    let realizedPl: Double?
    let winRate: Double?
    let reservedCash: Double?

    enum CodingKeys: String, CodingKey {
        case configured, paper, model, settings, equity, cash, holdings
        case accountError = "account_error"
        case closedTrades = "closed_trades"
        case realizedPl   = "realized_pl"
        case winRate      = "win_rate"
        case reservedCash = "reserved_cash"
    }
}

// MARK: - Decision log

struct PureAIDecision: Identifiable, Decodable {
    let id = UUID()
    let timestamp: String
    let model: String?
    let searches: [String]?
    let decision: PureAIDecisionPayload?
    let executed: [PureAIExecutedOrder]?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case timestamp, model, searches, decision, executed, error
        // `id` intentionally omitted — default UUID() used
    }
}

struct PureAIDecisionPayload: Decodable {
    let marketView: String?
    let buys: [PureAIOrder]?
    let sells: [PureAISell]?
    let adds: [PureAIOrder]?

    enum CodingKeys: String, CodingKey {
        case marketView = "market_view"
        case buys, sells, adds
    }
}

struct PureAIOrder: Identifiable, Decodable {
    let id = UUID()
    let symbol: String
    let dollars: Double?
    let thesis: String?

    enum CodingKeys: String, CodingKey {
        case symbol, dollars, thesis
    }
}

struct PureAISell: Identifiable, Decodable {
    let id = UUID()
    let symbol: String
    let portion: String?   // "all" or "0.5" — backend may send string or number
    let reason: String?

    enum CodingKeys: String, CodingKey {
        case symbol, portion, reason
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        symbol = try c.decode(String.self, forKey: .symbol)
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        // portion arrives as "all" (string) or 0.5 (number) — normalise to string
        if let s = try? c.decodeIfPresent(String.self, forKey: .portion) {
            portion = s
        } else if let n = try? c.decodeIfPresent(Double.self, forKey: .portion) {
            portion = String(n)
        } else {
            portion = nil
        }
    }
}

struct PureAIExecutedOrder: Identifiable, Decodable {
    let id = UUID()
    let action: String
    let symbol: String
    let qty: Double?
    let dollars: Double?
    let status: String
    let why: String?
    let reason: String?
    let thesis: String?
    let orderId: String?

    enum CodingKeys: String, CodingKey {
        case action, symbol, qty, dollars, status, why, reason, thesis
        case orderId = "order_id"
    }

    var statusColor: String {
        switch status {
        case "submitted": return "green"
        case "rejected":  return "orange"
        case "error":     return "red"
        default:          return "gray"
        }
    }
}

// MARK: - Cycle result

struct PureAICycleResult: Decodable {
    let status: String
    let error: String?
    let searches: [String]?
    let marketView: String?
    let executed: [PureAIExecutedOrder]?

    enum CodingKeys: String, CodingKey {
        case status, error, searches, executed
        case marketView = "market_view"
    }
}
