import Foundation

struct PortfolioPoint: Codable, Identifiable {
    var id: Int { Int(timestamp) }
    let timestamp: Double
    let equity: Double
    let profitLoss: Double
    let profitLossPct: Double

    enum CodingKeys: String, CodingKey {
        case timestamp, equity
        case profitLoss = "profit_loss"
        case profitLossPct = "profit_loss_pct"
    }

    var date: Date { Date(timeIntervalSince1970: timestamp) }
}
