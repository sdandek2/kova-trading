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

    // Safe defaults — if any field is missing the report still renders
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        portfolioValue   = (try? c.decode(Double.self, forKey: .portfolioValue))   ?? 0
        dayPl            = (try? c.decode(Double.self, forKey: .dayPl))            ?? 0
        dayPlPct         = (try? c.decode(Double.self, forKey: .dayPlPct))         ?? 0
        tradesExecuted   = (try? c.decode(Int.self,    forKey: .tradesExecuted))   ?? 0
        positionsClosed  = (try? c.decode(Int.self,    forKey: .positionsClosed))  ?? 0
        entriesRejected  = (try? c.decode(Int.self,    forKey: .entriesRejected))  ?? 0
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
    let botRating: String   // Claude sometimes returns Int, sometimes String — handled below

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

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        headline          = (try? c.decode(String.self, forKey: .headline))          ?? "—"
        performanceGrade  = (try? c.decode(String.self, forKey: .performanceGrade))  ?? "N/A"
        whatWorked        = (try? c.decode([String].self, forKey: .whatWorked))       ?? []
        whatDidnt         = (try? c.decode([String].self, forKey: .whatDidnt))        ?? []
        keyInsight        = (try? c.decode(String.self, forKey: .keyInsight))         ?? ""
        tomorrowWatchlist = (try? c.decode([EODWatchItem].self, forKey: .tomorrowWatchlist)) ?? []
        riskNote          = (try? c.decode(String.self, forKey: .riskNote))           ?? ""

        // bot_rating: Claude may return Int (8) or String ("8") — accept both
        if let s = try? c.decode(String.self, forKey: .botRating) {
            botRating = s
        } else if let i = try? c.decode(Int.self, forKey: .botRating) {
            botRating = "\(i)"
        } else {
            botRating = "N/A"
        }
    }
}

struct EODWatchItem: Decodable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let thesis: String
    let action: String

    // Safe defaults in case Claude omits a field
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        symbol = (try? c.decode(String.self, forKey: .symbol)) ?? "?"
        thesis = (try? c.decode(String.self, forKey: .thesis)) ?? ""
        action = (try? c.decode(String.self, forKey: .action)) ?? "watch"
    }

    enum CodingKeys: String, CodingKey {
        case symbol, thesis, action
    }
}
