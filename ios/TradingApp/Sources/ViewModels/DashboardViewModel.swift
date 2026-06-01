import Foundation
import Combine

@MainActor
class DashboardViewModel: ObservableObject {
    @Published var account: AccountInfo?
    @Published var positions: [Position] = []
    @Published var portfolioHistory: [PortfolioPoint] = []
    @Published var postChangeComparison: PostChangeComparison?
    @Published var isLoading = false
    @Published var errorMessage: String?

    private var cancellables = Set<AnyCancellable>()

    init() {
        WebSocketService.shared.$latestMessage
            .compactMap { $0 }
            .sink { [weak self] message in
                if case .positionUpdate(let positions) = message {
                    self?.positions = positions
                }
            }
            .store(in: &cancellables)
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        do {
            async let accountTask = APIService.shared.getAccount()
            async let positionsTask = APIService.shared.getPositions()
            async let historyTask = APIService.shared.getPortfolioHistory(period: "1W")
            async let comparisonTask = APIService.shared.getPostChangeComparison()
            let (acc, pos, history, comparison) = try await (accountTask, positionsTask, historyTask, comparisonTask)
            account = acc
            positions = pos
            portfolioHistory = history
            postChangeComparison = comparison
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    func loadPortfolioHistory(period: String) async {
        do {
            portfolioHistory = try await APIService.shared.getPortfolioHistory(period: period)
        } catch {
            // Non-fatal — keep existing chart data
        }
    }
}
