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

    /// Custom decoder so a pick missing any string field doesn't crash the whole response.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        symbol                = try c.decode(String.self, forKey: .symbol)
        type                  = (try c.decodeIfPresent(String.self, forKey: .type)) ?? "momentum"
        direction             = try c.decodeIfPresent(String.self, forKey: .direction)
        current_price_approx  = try c.decodeIfPresent(Double.self, forKey: .current_price_approx)
        upside_pct            = try c.decodeIfPresent(Double.self, forKey: .upside_pct)
        target_price          = try c.decodeIfPresent(Double.self, forKey: .target_price)
        time_horizon          = (try c.decodeIfPresent(String.self, forKey: .time_horizon)) ?? ""
        confidence            = (try c.decodeIfPresent(String.self, forKey: .confidence)) ?? "medium"
        thesis                = (try c.decodeIfPresent(String.self, forKey: .thesis)) ?? ""
        entry_zone            = (try c.decodeIfPresent(String.self, forKey: .entry_zone)) ?? ""
        invalidation          = (try c.decodeIfPresent(String.self, forKey: .invalidation)) ?? ""
        key_risk              = (try c.decodeIfPresent(String.self, forKey: .key_risk)) ?? ""
    }
}

struct AvoidItem: Codable, Identifiable {
    var id: String { symbol_or_sector }
    let symbol_or_sector: String
    let reason: String

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        symbol_or_sector = (try c.decodeIfPresent(String.self, forKey: .symbol_or_sector)) ?? ""
        reason           = (try c.decodeIfPresent(String.self, forKey: .reason)) ?? ""
    }
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

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        date          = (try c.decodeIfPresent(String.self, forKey: .date)) ?? ""
        generated_at  = (try c.decodeIfPresent(String.self, forKey: .generated_at)) ?? ""
        market_regime = (try c.decodeIfPresent(String.self, forKey: .market_regime)) ?? "unknown"
        geo_risk_level = (try c.decodeIfPresent(String.self, forKey: .geo_risk_level)) ?? "unknown"
        short_term    = (try c.decodeIfPresent([DailyPick].self, forKey: .short_term)) ?? []
        long_term     = (try c.decodeIfPresent([DailyPick].self, forKey: .long_term)) ?? []
        bearish_plays = try c.decodeIfPresent([DailyPick].self, forKey: .bearish_plays)
        avoid_today   = (try c.decodeIfPresent([AvoidItem].self, forKey: .avoid_today)) ?? []
        summary       = (try c.decodeIfPresent(String.self, forKey: .summary)) ?? ""
        error         = try c.decodeIfPresent(String.self, forKey: .error)
    }
}
