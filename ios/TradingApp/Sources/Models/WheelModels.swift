import Foundation

// ── Wheel Bot Models ──────────────────────────────────────────────────────────
// All types used by the iOS Wheel tab.
// Completely separate from Kova trading models.

// MARK: - Wheel Position

struct WheelPosition: Identifiable, Decodable {
    let id: Int
    let symbol: String
    let phase: String               // "put_open" | "assigned" | "call_open" | "bear_spread_open" | "completed"
    let putContract: String?
    let putStrike: Double?
    let putExpiry: String?
    let putPremium: Double?
    let sharesQty: Int?
    let costBasis: Double?
    let callContract: String?
    let callStrike: Double?
    let callExpiry: String?
    let callPremium: Double?
    let totalPremiumCollected: Double?
    let regimeAtOpen: String?
    let openedAt: String?
    let closedAt: String?
    let realizedPl: Double?
    let status: String
    let notes: String?

    enum CodingKeys: String, CodingKey {
        case id, symbol, phase, status, notes
        case putContract = "put_contract"
        case putStrike = "put_strike"
        case putExpiry = "put_expiry"
        case putPremium = "put_premium"
        case sharesQty = "shares_qty"
        case costBasis = "cost_basis"
        case callContract = "call_contract"
        case callStrike = "call_strike"
        case callExpiry = "call_expiry"
        case callPremium = "call_premium"
        case totalPremiumCollected = "total_premium_collected"
        case regimeAtOpen = "regime_at_open"
        case openedAt = "opened_at"
        case closedAt = "closed_at"
        case realizedPl = "realized_pl"
    }

    // Convenience
    var phaseLabel: String {
        switch phase {
        case "put_open":         return "Selling Put"
        case "assigned":         return "Assigned"
        case "call_open":        return "Selling Call"
        case "bear_spread_open": return "Bear Spread"
        case "completed":        return "Complete"
        default:                 return phase.capitalized
        }
    }

    var phaseColor: String {
        switch phase {
        case "put_open":         return "blue"
        case "assigned":         return "orange"
        case "call_open":        return "green"
        case "bear_spread_open": return "red"
        case "completed":        return "gray"
        default:                 return "gray"
        }
    }

    /// Current contract premium (put or call depending on phase)
    var currentPremium: Double? {
        switch phase {
        case "put_open":         return putPremium
        case "call_open":        return callPremium
        default:                 return nil
        }
    }

    /// Current expiry string
    var currentExpiry: String? {
        switch phase {
        case "put_open":         return putExpiry
        case "call_open":        return callExpiry
        default:                 return nil
        }
    }

    /// Number of contracts remaining (parsed from notes)
    var contractsRemaining: Int {
        guard let notes = notes else { return 2 }
        for part in notes.split(separator: " ") {
            if part.hasPrefix("contracts_remaining:"),
               let n = Int(part.dropFirst("contracts_remaining:".count)) {
                return n
            }
        }
        return 2
    }
}

// MARK: - Wheel Summary

struct WheelSummary: Decodable {
    let totalPremiumCollected: Double
    let totalRealizedPl: Double
    let activeCycles: Int
    let completedCycles: Int
    let winRate: Double?
    let avgCycleDays: Double?

    enum CodingKeys: String, CodingKey {
        case totalPremiumCollected = "total_premium_collected"
        case totalRealizedPl = "total_realized_pl"
        case activeCycles = "active_cycles"
        case completedCycles = "completed_cycles"
        case winRate = "win_rate"
        case avgCycleDays = "avg_cycle_days"
    }
}

// MARK: - Wheel Universe Stock

struct WheelUniverseStock: Identifiable, Decodable {
    let id = UUID()
    let symbol: String
    let score: Int
    let reason: String?
    let ivProfile: String?
    let isActive: Bool?
    let addedAt: String?

    enum CodingKeys: String, CodingKey {
        case symbol, score, reason
        case ivProfile = "iv_profile"
        case isActive = "is_active"
        case addedAt = "added_at"
    }

    var scoreColor: String {
        if score >= 80 { return "green" }
        if score >= 60 { return "yellow" }
        return "red"
    }
}

// MARK: - Wheel Config

struct WheelConfig: Decodable {
    let mode: String           // "paper" | "live"
    let usingDedicatedWheelKeys: Bool
    let maxActivePositions: Int
    let minPremiumYieldPct: Double
    let minDte: Int
    let maxDte: Int
    let targetDelta: Double
    let schedule: WheelSchedule?

    enum CodingKeys: String, CodingKey {
        case mode
        case usingDedicatedWheelKeys = "using_dedicated_wheel_keys"
        case maxActivePositions = "max_active_positions"
        case minPremiumYieldPct = "min_premium_yield_pct"
        case minDte = "min_dte"
        case maxDte = "max_dte"
        case targetDelta = "target_delta"
        case schedule
    }
}

struct WheelSchedule: Decodable {
    let universeRefresh: String?
    let dailyCycle: String?
    let optimizer: String?

    enum CodingKeys: String, CodingKey {
        case universeRefresh = "universe_refresh"
        case dailyCycle = "daily_cycle"
        case optimizer
    }
}

// MARK: - Wheel Status (full dashboard)

struct WheelStatus: Decodable {
    let regime: String
    let mode: String
    let activePositions: [WheelPosition]
    let activeCount: Int
    let maxPositions: Int
    let summary: WheelSummary
    let profitReserve: Double?
    let universe: [WheelUniverseStock]
    let universeCount: Int

    let account: WheelAccountInfo?

    enum CodingKeys: String, CodingKey {
        case regime, mode, account
        case activePositions = "active_positions"
        case activeCount = "active_count"
        case maxPositions = "max_positions"
        case summary
        case profitReserve = "profit_reserve"
        case universe
        case universeCount = "universe_count"
    }
}

// MARK: - Wheel Account Balance

struct WheelAccountInfo: Decodable {
    let portfolioValue: Double
    let cash: Double
    let buyingPower: Double
    let equity: Double

    enum CodingKeys: String, CodingKey {
        case portfolioValue = "portfolio_value"
        case cash
        case buyingPower = "buying_power"
        case equity
    }
}

// MARK: - Wheel Opportunity (scan preview)

struct WheelOpportunity: Identifiable, Decodable {
    let id = UUID()
    let symbol: String
    let stockPrice: Double
    let contract: String
    let strike: Double
    let expiry: String
    let dte: Int
    let premium: Double
    let premiumYieldPct: Double
    let annualYieldPct: Double
    let collateral: Double
    let contracts: Int?
    let iv: Double?
    let delta: Double?
    let sector: String?
    let regime: String
    let mode: String

    enum CodingKeys: String, CodingKey {
        case symbol, contract, strike, expiry, dte, premium, iv, delta, sector, regime, mode
        case stockPrice = "stock_price"
        case premiumYieldPct = "premium_yield_pct"
        case annualYieldPct = "annual_yield_pct"
        case collateral
        case contracts
    }
}
