import Foundation

enum APIError: LocalizedError {
    case invalidURL
    case httpError(Int)
    case decodingError(Error)
    case networkError(Error)

    var errorDescription: String? {
        switch self {
        case .invalidURL: return "Invalid URL"
        case .httpError(let code): return "HTTP error \(code)"
        case .decodingError(let e): return "Decoding error: \(e.localizedDescription)"
        case .networkError(let e): return e.localizedDescription
        }
    }
}

class APIService {
    static let shared = APIService()
    private let baseURL = Config.backendURL

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()

    private func fetch<T: Decodable>(_ path: String, method: String = "GET", timeout: TimeInterval = 10) async throws -> T {
        guard let url = URL(string: baseURL + path) else { throw APIError.invalidURL }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.timeoutInterval = timeout

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch {
            throw APIError.networkError(error)
        }

        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            throw APIError.httpError(http.statusCode)
        }

        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decodingError(error)
        }
    }

    func getAccount() async throws -> AccountInfo {
        try await fetch("/api/account")
    }

    func getPositions() async throws -> [Position] {
        try await fetch("/api/positions")
    }

    func getOrders() async throws -> [Order] {
        try await fetch("/api/orders")
    }

    func getTradingStatus() async throws -> TradingStatus {
        try await fetch("/api/trading/status")
    }

    func getAIAnalysis() async throws -> AIAnalysis {
        try await fetch("/api/trading/analysis")
    }

    func startTrading() async throws {
        let _: [String: String] = try await fetch("/api/trading/start", method: "POST")
    }

    func stopTrading() async throws {
        let _: [String: String] = try await fetch("/api/trading/stop", method: "POST")
    }

    func getNews() async throws -> [NewsArticle] {
        try await fetch("/api/news")
    }

    func getPortfolioHistory(period: String = "1W") async throws -> [PortfolioPoint] {
        try await fetch("/api/portfolio/history?period=\(period)")
    }

    func getPerformance() async throws -> PerformanceStats {
        try await fetch("/api/performance")
    }

    func getStrategy() async throws -> String {
        let response: [String: String] = try await fetch("/api/strategy/")
        return response["key"] ?? "aggressive"
    }

    func setStrategy(_ key: String) async throws {
        let _: [String: String] = try await fetch("/api/strategy/set/\(key)", method: "POST")
    }

    func getAllStrategies() async throws -> [[String: String]] {
        try await fetch("/api/strategy/all")
    }

    func getSuggestions() async throws -> [Suggestion] {
        let response: SuggestionsResponse = try await fetch("/api/predictions/suggestions", timeout: 90)
        return response.suggestions
    }

    func getStockPrediction(symbol: String) async throws -> StockPrediction {
        try await fetch("/api/predictions/\(symbol)", timeout: 90)
    }

    func getDailyPicks(refresh: Bool = false) async throws -> DailyPicksResponse {
        try await fetch("/api/picks/daily\(refresh ? "?refresh=true" : "")", timeout: 120)
    }

    func getWatchlist() async throws -> [String] {
        let response: [String: [String]] = try await fetch("/api/watchlist/")
        return response["watchlist"] ?? []
    }

    func setWatchlist(_ symbols: [String]) async throws {
        guard let url = URL(string: baseURL + "/api/watchlist/") else { throw APIError.invalidURL }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["watchlist": symbols])
        let (_, _) = try await URLSession.shared.data(for: request)
    }
}
