import Foundation

struct DailyPick: Codable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let type: String
    let direction: String?          // "long" | "short" | "inverse_etf"
    let current_price_approx: Double?
    let upside_pct: Double?
    let target_price: Double?
    let time_horizon: String
    let confidence: String
    let thesis: String
    let entry_zone: String
    let invalidation: String
    let key_risk: String

    var isBearish: Bool { direction == "short" || direction == "inverse_etf" }
    var isInverseETF: Bool { direction == "inverse_etf" }
}

struct AvoidItem: Codable, Identifiable {
    var id: String { symbol_or_sector }
    let symbol_or_sector: String
    let reason: String
}

struct DailyPicksResponse: Codable {
    let date: String
    let generated_at: String
    let market_regime: String
    let geo_risk_level: String
    let short_term: [DailyPick]
    let long_term: [DailyPick]
    let bearish_plays: [DailyPick]?
    let avoid_today: [AvoidItem]
    let summary: String
    let error: String?
}
