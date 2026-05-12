import Foundation
import Combine

@MainActor
class TradingViewModel: ObservableObject {
    @Published var status: TradingStatus?
    @Published var analysis: AIAnalysis?
    @Published var isLoading = false
    @Published var errorMessage: String?
    @Published var countdown: Int = 0

    private var countdownTimer: Timer?
    private var cancellables = Set<AnyCancellable>()

    init() {
        WebSocketService.shared.$latestMessage
            .compactMap { $0 }
            .sink { [weak self] message in
                if case .aiAnalysis(let a) = message {
                    self?.analysis = a
                }
            }
            .store(in: &cancellables)
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        do {
            async let statusTask = APIService.shared.getTradingStatus()
            async let analysisTask: AIAnalysis? = try? APIService.shared.getAIAnalysis()
            let (s, a) = try await (statusTask, analysisTask)
            status = s
            analysis = a
            startCountdown(from: s.nextRunInSeconds ?? 0)
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }

    func toggleBot() async {
        guard let status else { return }
        do {
            if status.isRunning {
                try await APIService.shared.stopTrading()
            } else {
                try await APIService.shared.startTrading()
            }
            await load()
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func startCountdown(from seconds: Int) {
        countdownTimer?.invalidate()
        countdown = seconds
        countdownTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if self.countdown > 0 {
                    self.countdown -= 1
                }
            }
        }
    }

    deinit {
        countdownTimer?.invalidate()
    }
}
