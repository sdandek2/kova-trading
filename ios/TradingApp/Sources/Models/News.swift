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

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id        = (try? c.decodeIfPresent(String.self, forKey: .id)) ?? UUID().uuidString
        headline  = (try? c.decodeIfPresent(String.self, forKey: .headline)) ?? ""
        summary   = (try? c.decodeIfPresent(String.self, forKey: .summary)) ?? ""
        author    = (try? c.decodeIfPresent(String.self, forKey: .author)) ?? ""
        url       = (try? c.decodeIfPresent(String.self, forKey: .url)) ?? ""
        symbols   = (try? c.decodeIfPresent([String].self, forKey: .symbols)) ?? []
        source    = (try? c.decodeIfPresent(String.self, forKey: .source)) ?? ""
        createdAt = try? c.decodeIfPresent(Date.self, forKey: .createdAt)
    }
}
