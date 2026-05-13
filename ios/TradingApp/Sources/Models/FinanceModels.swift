import Foundation

// MARK: - Tax Summary

struct TaxSummary: Decodable {
    let year: Int
    let ytd: TaxYTD
    let tax: TaxEstimate
    let brackets: [TaxBracket]
}

struct TaxYTD: Decodable {
    let totalGains: Double
    let totalLosses: Double
    let netPl: Double
    let tradeCount: Int
    let winningTrades: Int
    let losingTrades: Int

    enum CodingKeys: String, CodingKey {
        case totalGains = "total_gains"
        case totalLosses = "total_losses"
        case netPl = "net_pl"
        case tradeCount = "trade_count"
        case winningTrades = "winning_trades"
        case losingTrades = "losing_trades"
    }
}

struct TaxEstimate: Decodable {
    let bracketRate: Double
    let taxableGain: Double
    let estimatedTax: Double
    let afterTaxGain: Double
    let gainType: String
    let disclaimer: String

    enum CodingKeys: String, CodingKey {
        case bracketRate = "bracket_rate"
        case taxableGain = "taxable_gain"
        case estimatedTax = "estimated_tax"
        case afterTaxGain = "after_tax_gain"
        case gainType = "gain_type"
        case disclaimer
    }
}

struct TaxBracket: Decodable, Identifiable {
    var id: String { label }
    let label: String
    let rate: Double
    let description: String
}

// MARK: - Profit Reserve

struct ReserveStatus: Decodable {
    let reservedCash: Double
    let profitReservePct: Double
    let enabled: Bool
    let message: String

    enum CodingKeys: String, CodingKey {
        case reservedCash = "reserved_cash"
        case profitReservePct = "profit_reserve_pct"
        case enabled
        case message
    }
}

struct ReserveResetResponse: Decodable {
    let withdrawn: Double
    let newBalance: Double
    let message: String

    enum CodingKeys: String, CodingKey {
        case withdrawn
        case newBalance = "new_balance"
        case message
    }
}
