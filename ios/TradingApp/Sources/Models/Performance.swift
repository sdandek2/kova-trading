import Foundation

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

struct PostChangeComparison: Codable {
    let changeDate: String
    let baselineDays: Int
    let postDays: Int
    let baseline: PostChangeWindow
    let post: PostChangeWindow
    let delta: PostChangeDelta
    let assessment: PostChangeAssessment

    enum CodingKeys: String, CodingKey {
        case changeDate = "change_date"
        case baselineDays = "baseline_days"
        case postDays = "post_days"
        case baseline, post, delta, assessment
    }
}

struct PostChangeWindow: Codable {
    let startDate: String?
    let endDate: String?
    let daysRequested: Int
    let equityPoints: Int
    let startPortfolioValue: Double?
    let endPortfolioValue: Double?
    let portfolioReturnPct: Double?
    let portfolioReturnAbs: Double?
    let trades: Int
    let winRatePct: Double
    let avgPlPct: Double
    let avgWinnerPct: Double
    let avgLoserPct: Double
    let expectancyPct: Double
    let profitFactor: Double
    let totalRealizedPl: Double

    enum CodingKeys: String, CodingKey {
        case startDate = "start_date"
        case endDate = "end_date"
        case daysRequested = "days_requested"
        case equityPoints = "equity_points"
        case startPortfolioValue = "start_portfolio_value"
        case endPortfolioValue = "end_portfolio_value"
        case portfolioReturnPct = "portfolio_return_pct"
        case portfolioReturnAbs = "portfolio_return_abs"
        case trades
        case winRatePct = "win_rate_pct"
        case avgPlPct = "avg_pl_pct"
        case avgWinnerPct = "avg_winner_pct"
        case avgLoserPct = "avg_loser_pct"
        case expectancyPct = "expectancy_pct"
        case profitFactor = "profit_factor"
        case totalRealizedPl = "total_realized_pl"
    }
}

struct PostChangeDelta: Codable {
    let portfolioReturnPct: Double?
    let expectancyPct: Double
    let avgPlPct: Double
    let winRatePct: Double
    let profitFactor: Double
    let totalRealizedPl: Double
    let tradeCount: Int

    enum CodingKeys: String, CodingKey {
        case portfolioReturnPct = "portfolio_return_pct"
        case expectancyPct = "expectancy_pct"
        case avgPlPct = "avg_pl_pct"
        case winRatePct = "win_rate_pct"
        case profitFactor = "profit_factor"
        case totalRealizedPl = "total_realized_pl"
        case tradeCount = "trade_count"
    }
}

struct PostChangeAssessment: Codable {
    let status: String
    let summary: String
}
