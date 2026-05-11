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

    /// Resolve a company name or partial ticker to a list of matching ticker symbols.
    /// e.g. "Apple" → [TickerResult(symbol: "AAPL", name: "Apple Inc.", exchange: "NASDAQ")]
    func searchTicker(query: String) async throws -> [TickerResult] {
        guard let encoded = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) else { return [] }
        let response: TickerSearchResponse = try await fetch("/api/predictions/search?q=\(encoded)", timeout: 10)
        return response.results
    }

    /// Convenience: returns the top ticker symbol for a query, or nil if nothing found.
    func resolveToTicker(_ input: String) async -> String? {
        let trimmed = input.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        // If it already looks like a ticker (short, no spaces), use it directly
        if trimmed.count <= 5 && !trimmed.contains(" ") && trimmed == trimmed.uppercased() {
            return trimmed
        }
        // Otherwise search by company name
        if let results = try? await searchTicker(query: trimmed), let first = results.first {
            return first.symbol
        }
        return trimmed.uppercased() // fallback: treat as ticker
    }

    func getDailyPicks(refresh: Bool = false) async throws -> DailyPicksResponse {
        try await fetch("/api/picks/daily\(refresh ? "?refresh=true" : "")", timeout: 120)
    }

    func cancelOrder(id: String) async throws {
        guard let url = URL(string: baseURL + "/api/orders/\(id)") else { throw APIError.invalidURL }
        var request = URLRequest(url: url)
        request.httpMethod = "DELETE"
        request.timeoutInterval = 10
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw APIError.networkError(NSError(domain: "", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: detail ?? "Cancel failed"]))
        }
    }

    func placeManualOrder(symbol: String, side: String, qty: Int) async throws {
        guard let url = URL(string: baseURL + "/api/orders/manual") else { throw APIError.invalidURL }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["symbol": symbol, "side": side, "qty": qty])
        let (data, response) = try await URLSession.shared.data(for: request)
        if let http = response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["detail"] as? String
            throw APIError.networkError(NSError(domain: "", code: http.statusCode, userInfo: [NSLocalizedDescriptionKey: detail ?? "Order failed"]))
        }
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
