import Foundation

struct Order: Codable, Identifiable {
    let id: String
    let symbol: String
    let side: String
    let qty: Double
    let status: String
    let filledAvgPrice: Double?
    let createdAt: Date

    enum CodingKeys: String, CodingKey {
        case id, symbol, side, qty, status
        case filledAvgPrice = "filled_avg_price"
        case createdAt = "created_at"
    }
}
