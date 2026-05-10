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
