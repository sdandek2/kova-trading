import SwiftUI

struct NewsView: View {
    @StateObject private var vm = NewsViewModel()

    var body: some View {
        NavigationStack {
            Group {
                if vm.isLoading && vm.articles.isEmpty {
                    LoadingView()
                } else if let error = vm.errorMessage, vm.articles.isEmpty {
                    ErrorView(message: error) { await vm.load() }
                } else if vm.articles.isEmpty {
                    VStack(spacing: 12) {
                        Image(systemName: "newspaper")
                            .font(.system(size: 48))
                            .foregroundStyle(.secondary)
                        Text("No News")
                            .font(.headline)
                        Text("Market news will appear here.")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    .padding()
                } else {
                    List(vm.articles) { article in
                        Group {
                            if let url = URL(string: article.url), !article.url.isEmpty {
                                Link(destination: url) {
                                    NewsRowView(article: article)
                                }
                                .foregroundStyle(.primary)
                            } else {
                                NewsRowView(article: article)
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                    .refreshable { await vm.load() }
                }
            }
            .navigationTitle("Market News")
        }
        .task { await vm.load() }
    }
}
