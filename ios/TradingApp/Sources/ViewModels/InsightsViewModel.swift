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

    func loadDailyPicks(refresh: Bool = false) async {
        isLoadingDailyPicks = true
        dailyPicksError = nil
        do {
            dailyPicks = try await APIService.shared.getDailyPicks(refresh: refresh)
        } catch {
            dailyPicksError = error.localizedDescription
        }
        isLoadingDailyPicks = false
    }

    func loadSuggestions() async {
        isLoadingSuggestions = true
        suggestionsError = nil
        do {
            suggestions = try await APIService.shared.getSuggestions()
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
        } catch {
            predictionError = error.localizedDescription
        }
        isLoadingPrediction = false
    }
}
