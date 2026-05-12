import Foundation
import Combine

@MainActor
class DashboardViewModel: ObservableObject {
    @Published var account: AccountInfo?
    @Published var positions: [Position] = []
    @Published var portfolioHistory: [PortfolioPoint] = []
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
            let (acc, pos, history) = try await (accountTask, positionsTask, historyTask)
            account = acc
            positions = pos
            portfolioHistory = history
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
