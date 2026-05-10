import Foundation

struct NewsArticle: Codable, Identifiable {
    let id: String
    let headline: String
    let summary: String
    let author: String
    let createdAt: Date?
    let url: String
    let symbols: [String]
    let source: String

    enum CodingKeys: String, CodingKey {
        case id, headline, summary, author, url, symbols, source
        case createdAt = "created_at"
    }
}
