import Foundation

struct PriceTarget: Codable {
    let price: Double?
    let change_pct: Double?
    let rationale: String

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        price = try c.decodeIfPresent(Double.self, forKey: .price)
        change_pct = try c.decodeIfPresent(Double.self, forKey: .change_pct)
        rationale = (try c.decodeIfPresent(String.self, forKey: .rationale)) ?? ""
    }
}

struct PriceTargets: Codable {
    let week_1: PriceTarget
    let month_1: PriceTarget
    let month_3: PriceTarget

    static let empty = PriceTargets(
        week_1: PriceTarget(price: nil, change_pct: nil, rationale: ""),
        month_1: PriceTarget(price: nil, change_pct: nil, rationale: ""),
        month_3: PriceTarget(price: nil, change_pct: nil, rationale: "")
    )
    init(week_1: PriceTarget, month_1: PriceTarget, month_3: PriceTarget) {
        self.week_1 = week_1; self.month_1 = month_1; self.month_3 = month_3
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        week_1 = (try c.decodeIfPresent(PriceTarget.self, forKey: .week_1)) ?? PriceTarget(price: nil, change_pct: nil, rationale: "")
        month_1 = (try c.decodeIfPresent(PriceTarget.self, forKey: .month_1)) ?? PriceTarget(price: nil, change_pct: nil, rationale: "")
        month_3 = (try c.decodeIfPresent(PriceTarget.self, forKey: .month_3)) ?? PriceTarget(price: nil, change_pct: nil, rationale: "")
    }
}

extension PriceTarget {
    init(price: Double?, change_pct: Double?, rationale: String) {
        self.price = price; self.change_pct = change_pct; self.rationale = rationale
    }
}

struct Scenario: Codable {
    let price_target: Double?
    let trigger: String
    let probability: String

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        price_target = try c.decodeIfPresent(Double.self, forKey: .price_target)
        trigger = (try c.decodeIfPresent(String.self, forKey: .trigger)) ?? ""
        probability = (try c.decodeIfPresent(String.self, forKey: .probability)) ?? ""
    }
}

struct Scenarios: Codable {
    let bull: Scenario
    let base: Scenario
    let bear: Scenario

    static let empty = Scenarios(
        bull: Scenario(price_target: nil, trigger: "", probability: ""),
        base: Scenario(price_target: nil, trigger: "", probability: ""),
        bear: Scenario(price_target: nil, trigger: "", probability: "")
    )
    init(bull: Scenario, base: Scenario, bear: Scenario) {
        self.bull = bull; self.base = base; self.bear = bear
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bull = (try c.decodeIfPresent(Scenario.self, forKey: .bull)) ?? Scenario(price_target: nil, trigger: "", probability: "")
        base = (try c.decodeIfPresent(Scenario.self, forKey: .base)) ?? Scenario(price_target: nil, trigger: "", probability: "")
        bear = (try c.decodeIfPresent(Scenario.self, forKey: .bear)) ?? Scenario(price_target: nil, trigger: "", probability: "")
    }
}

extension Scenario {
    init(price_target: Double?, trigger: String, probability: String) {
        self.price_target = price_target; self.trigger = trigger; self.probability = probability
    }
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

    /// Custom decoder with graceful fallbacks so a partial/unexpected AI response
    /// doesn't crash the detail view (e.g. delisted stocks, incomplete JSON).
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        symbol = try c.decode(String.self, forKey: .symbol)
        current_price = try c.decodeIfPresent(Double.self, forKey: .current_price)
        recommendation = (try c.decodeIfPresent(String.self, forKey: .recommendation)) ?? "hold"
        confidence = (try c.decodeIfPresent(String.self, forKey: .confidence)) ?? "low"
        technical_signal = (try c.decodeIfPresent(String.self, forKey: .technical_signal)) ?? "neutral"
        sentiment_signal = (try c.decodeIfPresent(String.self, forKey: .sentiment_signal)) ?? "neutral"
        macro_alignment = (try c.decodeIfPresent(String.self, forKey: .macro_alignment)) ?? "neutral"
        targets = (try c.decodeIfPresent(PriceTargets.self, forKey: .targets)) ?? PriceTargets.empty
        scenarios = (try c.decodeIfPresent(Scenarios.self, forKey: .scenarios)) ?? Scenarios.empty
        key_catalysts = (try c.decodeIfPresent([String].self, forKey: .key_catalysts)) ?? []
        key_risks = (try c.decodeIfPresent([String].self, forKey: .key_risks)) ?? []
        reasoning = (try c.decodeIfPresent(String.self, forKey: .reasoning)) ?? "Analysis unavailable."
        generated_at = try c.decodeIfPresent(String.self, forKey: .generated_at)
        cache_expires_at = try c.decodeIfPresent(String.self, forKey: .cache_expires_at)
    }
}

struct SuggestionsResponse: Codable {
    let suggestions: [Suggestion]
}

struct TickerResult: Codable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let name: String
    let exchange: String
}

struct TickerSearchResponse: Codable {
    let results: [TickerResult]
}

struct Suggestion: Codable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let type: String                 // momentum | defensive | geopolitical | contrarian | etf | short_candidate | inverse_etf | breakdown
    let direction: String?           // "long" | "short" | "inverse_etf" — nil means long for backward compat
    let horizon: String              // short_term | long_term | both
    let short_term_thesis: String
    let long_term_thesis: String
    let risk_level: String           // low | medium | high
    let entry_note: String
    let upside_pct: Double?
    let current_price: Double?
    let five_day_change_pct: Double?

    var tradeDirection: String { direction ?? "long" }
    var isShort: Bool { tradeDirection == "short" }
    var isInverseETF: Bool { tradeDirection == "inverse_etf" }
    var isBearish: Bool { isShort || isInverseETF }
    var prefillSide: String { isShort ? "short" : "buy" }
}
