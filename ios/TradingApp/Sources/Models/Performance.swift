import Foundation

struct SideScorecard: Codable {
    let trades: Int
    let winRate: Double?
    let avgWinnerPct: Double?
    let avgLoserPct: Double?
    let profitFactor: Double?
    let avgHoldMins: Double?

    enum CodingKeys: String, CodingKey {
        case trades
        case winRate = "win_rate"
        case avgWinnerPct = "avg_winner_pct"
        case avgLoserPct = "avg_loser_pct"
        case profitFactor = "profit_factor"
        case avgHoldMins = "avg_hold_mins"
    }
}

struct BlockedTradeReport: Codable, Identifiable {
    var id: String { blockReason }
    let blockReason: String
    let timesBlocked: Int
    let avgSignalScore: Double?
    let avgHypotheticalPnl: Double?
    let totalHypotheticalPnl: Double?
    let wouldHaveWon: Int
    let wouldHaveLost: Int

    enum CodingKeys: String, CodingKey {
        case blockReason = "block_reason"
        case timesBlocked = "times_blocked"
        case avgSignalScore = "avg_signal_score"
        case avgHypotheticalPnl = "avg_hypothetical_pnl"
        case totalHypotheticalPnl = "total_hypothetical_pnl"
        case wouldHaveWon = "would_have_won"
        case wouldHaveLost = "would_have_lost"
    }
}

struct PortfolioVaR: Codable {
    let varDollars: Double?
    let varPct: Double?
    let grossDollars: Double?
    let grossPct: Double?
    let asOf: String?

    enum CodingKeys: String, CodingKey {
        case varDollars = "var_dollars"
        case varPct = "var_pct"
        case grossDollars = "gross_dollars"
        case grossPct = "gross_pct"
        case asOf = "as_of"
    }
}

struct SetupPerformance: Codable, Identifiable {
    var id: String { setupType }
    let setupType: String
    let trades: Int
    let totalPl: Double
    let avgPlPct: Double
    let winRate: Double

    enum CodingKeys: String, CodingKey {
        case setupType = "setup_type"
        case trades
        case totalPl = "total_pl"
        case avgPlPct = "avg_pl_pct"
        case winRate = "win_rate"
    }
}

struct AIBaselineStats: Codable {
    let signalBuys: Int
    let claudeOverrides: Int
    let overrideRate: Double?
    let claudeWinRate: Double?
    let totalClosedTrades: Int

    enum CodingKeys: String, CodingKey {
        case signalBuys = "signal_buys"
        case claudeOverrides = "claude_overrides"
        case overrideRate = "override_rate"
        case claudeWinRate = "claude_win_rate"
        case totalClosedTrades = "total_closed_trades"
    }
}

struct SlippageSummary: Codable {
    let fills: Int
    let avgSlippageDollars: Double?
    let avgSlippagePct: Double?
    let totalSlippageCost: Double?
    let bestSlippage: Double?
    let worstSlippage: Double?

    enum CodingKeys: String, CodingKey {
        case fills
        case avgSlippageDollars = "avg_slippage_dollars"
        case avgSlippagePct = "avg_slippage_pct"
        case totalSlippageCost = "total_slippage_cost"
        case bestSlippage = "best_slippage"
        case worstSlippage = "worst_slippage"
    }
}

struct HourPerformance: Codable, Identifiable {
    var id: Int { hourEt }
    let hourEt: Int
    let trades: Int
    let winRate: Double
    let avgPlPct: Double

    enum CodingKeys: String, CodingKey {
        case hourEt = "entry_hour_et"
        case trades
        case winRate = "win_rate"
        case avgPlPct = "avg_pl_pct"
    }
}

struct PerformanceStats: Codable {
    let totalTrades: Int
    let winRate: Double
    let avgWin: Double
    let avgLoss: Double
    let profitFactor: Double
    let sharpeRatio: Double
    let portfolioReturn1m: Double?
    let spyReturn1m: Double?
    let alpha: Double?

    enum CodingKeys: String, CodingKey {
        case totalTrades = "total_trades"
        case winRate = "win_rate"
        case avgWin = "avg_win"
        case avgLoss = "avg_loss"
        case profitFactor = "profit_factor"
        case sharpeRatio = "sharpe_ratio"
        case portfolioReturn1m = "portfolio_return_1m"
        case spyReturn1m = "spy_return_1m"
        case alpha
    }
}
