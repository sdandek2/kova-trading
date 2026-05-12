import Foundation

struct EODReport: Decodable {
    let available: Bool
    let date: String?
    let generatedAt: String?
    let stats: EODStats?
    let analysis: EODAnalysis?
    let message: String?

    enum CodingKeys: String, CodingKey {
        case available, date, stats, analysis, message
        case generatedAt = "generated_at"
    }
}

struct EODStats: Decodable {
    let portfolioValue: Double
    let dayPl: Double
    let dayPlPct: Double
    let tradesExecuted: Int
    let positionsClosed: Int
    let entriesRejected: Int

    enum CodingKeys: String, CodingKey {
        case portfolioValue = "portfolio_value"
        case dayPl = "day_pl"
        case dayPlPct = "day_pl_pct"
        case tradesExecuted = "trades_executed"
        case positionsClosed = "positions_closed"
        case entriesRejected = "entries_rejected"
    }
}

struct EODAnalysis: Decodable {
    let headline: String
    let performanceGrade: String
    let whatWorked: [String]
    let whatDidnt: [String]
    let keyInsight: String
    let tomorrowWatchlist: [EODWatchItem]
    let riskNote: String
    let botRating: String

    enum CodingKeys: String, CodingKey {
        case headline
        case performanceGrade = "performance_grade"
        case whatWorked = "what_worked"
        case whatDidnt = "what_didnt"
        case keyInsight = "key_insight"
        case tomorrowWatchlist = "tomorrow_watchlist"
        case riskNote = "risk_note"
        case botRating = "bot_rating"
    }
}

struct EODWatchItem: Decodable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let thesis: String
    let action: String
}
