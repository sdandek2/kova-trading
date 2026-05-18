import Foundation

@MainActor
class InsightsViewModel: ObservableObject {
    @Published var dailyPicks: DailyPicksResponse? = nil
    @Published var suggestions: [Suggestion] = []
    @Published var prediction: StockPrediction? = nil
    @Published var isLoadingDailyPicks = false
    @Published var isLoadingSuggestions = false
    @Published var isLoadingPrediction = false
    @Published var dailyPicksError: String? = nil
    @Published var suggestionsError: String? = nil
    @Published var predictionError: String? = nil

    /// Live bid/ask midpoint prices fetched from Alpaca after suggestions/picks load.
    /// Views use this as the authoritative price, falling back to the cached value.
    @Published var livePrices: [String: Double] = [:]

    func loadDailyPicks(refresh: Bool = false) async {
        isLoadingDailyPicks = true
        dailyPicksError = nil
        do {
            dailyPicks = try await APIService.shared.getDailyPicks(refresh: refresh)
            // Refresh live prices for all displayed picks
            await refreshLivePricesForVisibleSymbols()
        } catch {
            dailyPicksError = error.localizedDescription
        }
        isLoadingDailyPicks = false
    }

    func loadSuggestions(refresh: Bool = false) async {
        isLoadingSuggestions = true
        suggestionsError = nil
        do {
            suggestions = try await APIService.shared.getSuggestions(refresh: refresh)
            // Refresh live prices for all suggestions
            await refreshLivePricesForVisibleSymbols()
        } catch {
            suggestionsError = error.localizedDescription
        }
        isLoadingSuggestions = false
    }

    func loadPrediction(symbol: String) async {
        guard !symbol.isEmpty else { return }
        isLoadingPrediction = true
        predictionError = nil
        prediction = nil
        do {
            prediction = try await APIService.shared.getStockPrediction(symbol: symbol.uppercased())
            // Fetch live price for this specific symbol
            let prices = await APIService.shared.getLivePrices(symbols: [symbol.uppercased()])
            livePrices.merge(prices) { _, new in new }
        } catch {
            predictionError = error.localizedDescription
        }
        isLoadingPrediction = false
    }

    /// Collect all symbols currently shown and fetch their live prices in one batch call.
    private func refreshLivePricesForVisibleSymbols() async {
        var symbols = Set<String>()
        suggestions.forEach { symbols.insert($0.symbol) }
        if let picks = dailyPicks {
            picks.short_term.forEach { symbols.insert($0.symbol) }
            picks.long_term.forEach { symbols.insert($0.symbol) }
            picks.bearish_plays?.forEach { symbols.insert($0.symbol) }
        }
        guard !symbols.isEmpty else { return }
        let prices = await APIService.shared.getLivePrices(symbols: Array(symbols))
        livePrices.merge(prices) { _, new in new }
    }
}
