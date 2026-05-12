import Foundation

struct PositionLog: Decodable, Identifiable {
    var id: String { "\(symbol)-\(exit_time ?? entry_time ?? "")" }
    let symbol: String
    let side: String?             // "long" | "short" — from DB side column
    let entry_time: String?
    let exit_time: String?
    let entry_price: Double?
    let exit_price: Double?
    let quantity: Int?
    let realized_pl: Double?
    let realized_pl_pct: Double?
    let hold_duration_mins: Int?
    let exit_reason: String?
    let strategy: String?
    let claude_reasoning: String?
    let market_regime: String?

    var isWin: Bool { (realized_pl ?? 0) > 0 }

    var holdDurationText: String {
        guard let mins = hold_duration_mins else { return "—" }
        if mins < 60 { return "\(mins)m" }
        let h = mins / 60; let m = mins % 60
        return m == 0 ? "\(h)h" : "\(h)h \(m)m"
    }

    var isShort: Bool { side == "short" || (side == nil && strategy?.contains("short") == true) }

    var exitReasonLabel: String {
        switch exit_reason {
        case "trailing_stop":  return "Trailing Stop"
        case "take_profit":    return "Take Profit"
        case "loss_cut":       return "Loss Cut"
        case "ai_sell":        return "AI Sell"
        case "scale_out":      return "Scale Out"
        case "short_cover":    return "Short Cover"
        default:               return exit_reason?.replacingOccurrences(of: "_", with: " ").capitalized ?? "Unknown"
        }
    }
}

struct PerformanceSummary: Decodable {
    let total_trades: Int
    let wins: Int
    let losses: Int
    let win_rate_pct: Double
    let avg_pl_pct: Double
    let avg_win_pct: Double
    let avg_loss_pct: Double
    let total_realized_pl: Double
    let best_symbols: [SymbolPerf]
    let worst_symbols: [SymbolPerf]
}

struct SymbolPerf: Decodable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let avg_pct: Double
}

struct BotActivity: Decodable, Identifiable {
    var id: String { "\(timestamp ?? "")-\(event_type)" }
    let timestamp: String?
    let cycle_id: String?
    let event_type: String
    let symbol: String?
    let message: String

    var eventColor: String {
        switch event_type {
        case "approved":        return "green"
        case "entry_rejected":  return "orange"
        case "earnings_block":  return "yellow"
        case "circuit_breaker": return "red"
        case "trailing_stop":   return "purple"
        case "scale_out":       return "blue"
        case "cover_short":     return "teal"
        case "position_closed": return "secondary"
        default:                return "secondary"
        }
    }

    var eventIcon: String {
        switch event_type {
        case "approved":        return "checkmark.circle.fill"
        case "entry_rejected":  return "xmark.circle"
        case "earnings_block":  return "calendar.badge.exclamationmark"
        case "circuit_breaker": return "exclamationmark.triangle.fill"
        case "trailing_stop":   return "arrow.down.circle.fill"
        case "scale_out":       return "arrow.up.right.circle"
        case "cover_short":     return "arrow.down.forward.circle.fill"
        case "position_closed": return "circle.fill"
        default:                return "info.circle"
        }
    }
}
