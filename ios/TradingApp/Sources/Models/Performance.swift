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

// MARK: - Near-Miss Tracker (Session 7)

struct NearMissSignal: Codable, Identifiable {
    var id: String { signal }
    let signal: String
    let timesWasDecidingFactor: Int

    enum CodingKeys: String, CodingKey {
        case signal
        case timesWasDecidingFactor = "times_was_deciding_factor"
    }
}

struct NearMissEntry: Codable, Identifiable {
    var id: String { "\(symbol)-\(timestamp ?? "")" }
    let symbol: String
    let score: Int
    let suggestedAction: String?
    let priceAtSkip: Double?
    let priceEod: Double?
    let hypotheticalPnlPct: Double?
    let wasRightToSkip: Bool?
    let timestamp: String?

    enum CodingKeys: String, CodingKey {
        case symbol, score, timestamp
        case suggestedAction = "suggested_action"
        case priceAtSkip = "price_at_skip"
        case priceEod = "price_eod"
        case hypotheticalPnlPct = "hypothetical_pnl_pct"
        case wasRightToSkip = "was_right_to_skip"
    }
}

struct NearMissSummaryData: Codable {
    let totalNearMisses: Int
    let wouldHaveBeenProfitable: Int
    let accuracyIfTraded: String
    let avgHypotheticalPnl: String
    let avgScore: Double?
    let thresholdVerdict: String

    enum CodingKeys: String, CodingKey {
        case totalNearMisses = "total_near_misses"
        case wouldHaveBeenProfitable = "would_have_been_profitable"
        case accuracyIfTraded = "accuracy_if_traded"
        case avgHypotheticalPnl = "avg_hypothetical_pnl"
        case avgScore = "avg_score"
        case thresholdVerdict = "threshold_verdict"
    }
}

struct NearMissReport: Codable {
    let summary: NearMissSummaryData
    let topMissedSignals: [NearMissSignal]
    let recent: [NearMissEntry]

    enum CodingKeys: String, CodingKey {
        case summary
        case topMissedSignals = "top_missed_signals"
        case recent
    }
}

// MARK: - Sprint Review (Session 7)

struct SprintMover: Codable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let pctChange: Double
    let kovaStatus: String   // CAPTURED | IN_UNIVERSE_SKIPPED | IN_UNIVERSE_WRONG | MISSED_ENTIRELY
    let kovaPnl: Double?
    let sources: [String]

    enum CodingKeys: String, CodingKey {
        case symbol
        case pctChange = "pct_change"
        case kovaStatus = "kova_status"
        case kovaPnl = "kova_pnl"
        case sources
    }
}

struct DailySprintReview: Codable {
    let reviewDate: String?
    let topGainers: [SprintMover]
    let topLosers: [SprintMover]
    let opportunityCaptureRate: Double?
    let hypotheticalLongPnl: Double?
    let hypotheticalShortPnl: Double?
    let actualKovaPnl: Double?
    let missedEntirelyCount: Int?
    let signalWinRates: [String: Double]?
    let flaggedSignals: [String]?

    enum CodingKeys: String, CodingKey {
        case reviewDate = "review_date"
        case topGainers = "top_gainers"
        case topLosers = "top_losers"
        case opportunityCaptureRate = "opportunity_capture_rate"
        case hypotheticalLongPnl = "hypothetical_long_pnl"
        case hypotheticalShortPnl = "hypothetical_short_pnl"
        case actualKovaPnl = "actual_kova_pnl"
        case missedEntirelyCount = "missed_entirely_count"
        case signalWinRates = "signal_win_rates"
        case flaggedSignals = "flagged_signals"
    }
}
