import Foundation

// MARK: - Connector Health

struct ConnectorHealth: Decodable, Identifiable {
    var id: String { connectorName }
    let connectorName: String
    let totalCalls: Int
    let failedCalls: Int
    let failurePct: Int
    let lastCalled: String?
    let lastError: String?

    enum CodingKeys: String, CodingKey {
        case connectorName = "connector_name"
        case totalCalls = "total_calls"
        case failedCalls = "failed_calls"
        case failurePct = "failure_pct"
        case lastCalled = "last_called"
        case lastError = "last_error"
    }

    var statusColor: String {
        if failurePct >= 80 { return "red" }
        if failurePct >= 40 { return "orange" }
        return "green"
    }

    var displayName: String {
        connectorName.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

// MARK: - Signal Weights

struct SignalWeightHistory: Decodable, Identifiable {
    var id: String { "\(newWeight)-\(oldWeight)-\(adjustedAt ?? "unknown")" }
    let oldWeight: Int
    let newWeight: Int
    let direction: String?
    let winRate: Double?
    let sampleCount: Int?
    let adjustedAt: String?

    enum CodingKeys: String, CodingKey {
        case oldWeight = "old_weight"
        case newWeight = "new_weight"
        case direction
        case winRate = "win_rate"
        case sampleCount = "sample_count"
        case adjustedAt = "adjusted_at"
    }
}

struct SignalWeight: Decodable, Identifiable {
    var id: String { signalName }
    let signalName: String
    let currentWeight: Int
    let defaultWeight: Int
    let pctOfDefault: Int
    let trend: String      // "up" | "down" | "flat"
    let lastAdjusted: String?
    let adjustmentReason: String?
    let history: [SignalWeightHistory]

    enum CodingKeys: String, CodingKey {
        case signalName = "signal_name"
        case currentWeight = "current_weight"
        case defaultWeight = "default_weight"
        case pctOfDefault = "pct_of_default"
        case trend
        case lastAdjusted = "last_adjusted"
        case adjustmentReason = "adjustment_reason"
        case history
    }

    var displayName: String {
        signalName
            .replacingOccurrences(of: "_", with: " ")
            .capitalized
    }
}

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
    let bracketDescription: String

    enum CodingKeys: String, CodingKey {
        case label, rate
        case bracketDescription = "description"
    }
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
