import Foundation

struct AccountInfo: Codable {
    let portfolioValue: Double
    let cash: Double
    let buyingPower: Double
    let tradeableCash: Double?
    let rawCash: Double?
    let dayPl: Double
    let dayPlPercent: Double

    enum CodingKeys: String, CodingKey {
        case portfolioValue = "portfolio_value"
        case cash
        case buyingPower = "buying_power"
        case tradeableCash = "tradeable_cash"
        case rawCash = "raw_cash"
        case dayPl = "day_pl"
        case dayPlPercent = "day_pl_percent"
    }
}

struct Position: Codable, Identifiable {
    var id: String { symbol }
    let symbol: String
    let qty: Double
    let side: String          // "long" | "short"
    let avgEntryPrice: Double
    let currentPrice: Double
    let unrealizedPl: Double
    let unrealizedPlPercent: Double
    let marketValue: Double

    var isShort: Bool { side == "short" }

    enum CodingKeys: String, CodingKey {
        case symbol, qty, side
        case avgEntryPrice = "avg_entry_price"
        case currentPrice = "current_price"
        case unrealizedPl = "unrealized_pl"
        case unrealizedPlPercent = "unrealized_pl_percent"
        case marketValue = "market_value"
    }
}
