import Foundation

struct PriceTarget: Codable {
    let price: Double?
    let change_pct: Double?
    let rationale: String
}

struct PriceTargets: Codable {
    let week_1: PriceTarget
    let month_1: PriceTarget
    let month_3: PriceTarget
}

struct Scenario: Codable {
    let price_target: Double?
    let trigger: String
    let probability: String
}

struct Scenarios: Codable {
    let bull: Scenario
    let base: Scenario
    let bear: Scenario
}

struct StockPrediction: Codable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let current_price: Double?
    let recommendation: String       // strong_buy | buy | hold | sell | strong_sell
    let confidence: String           // high | medium | low
    let technical_signal: String
    let sentiment_signal: String
    let macro_alignment: String
    let targets: PriceTargets
    let scenarios: Scenarios
    let key_catalysts: [String]
    let key_risks: [String]
    let reasoning: String
    let generated_at: String?
    let cache_expires_at: String?
}

struct SuggestionsResponse: Codable {
    let suggestions: [Suggestion]
}

struct Suggestion: Codable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let type: String                 // momentum | defensive | geopolitical | contrarian | etf
    let horizon: String              // short_term | long_term | both
    let short_term_thesis: String
    let long_term_thesis: String
    let risk_level: String           // low | medium | high
    let entry_note: String
    let upside_pct: Double?
    let current_price: Double?
    let five_day_change_pct: Double?
}
