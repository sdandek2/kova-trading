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
        defer { isLoading = false }
        async let accountTask: AccountInfo? = try? APIService.shared.getAccount()
        async let positionsTask: [Position]? = try? APIService.shared.getPositions()
        async let historyTask: [PortfolioPoint]? = try? APIService.shared.getPortfolioHistory(period: "1W")
        let (acc, pos, history) = await (accountTask, positionsTask, historyTask)
        if let acc { account = acc }
        if let pos { positions = pos }
        if let history { portfolioHistory = history }
    }

    func loadPortfolioHistory(period: String) async {
        do {
            portfolioHistory = try await APIService.shared.getPortfolioHistory(period: period)
        } catch {
            // Non-fatal — keep existing chart data
        }
    }
}
