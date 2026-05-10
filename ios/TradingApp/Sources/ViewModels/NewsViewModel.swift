import Foundation

@MainActor
class NewsViewModel: ObservableObject {
    @Published var articles: [NewsArticle] = []
    @Published var isLoading = false
    @Published var errorMessage: String?

    func load() async {
        isLoading = true
        errorMessage = nil
        do {
            articles = try await APIService.shared.getNews()
        } catch {
            errorMessage = error.localizedDescription
        }
        isLoading = false
    }
}
