import SwiftUI

struct WatchlistEditorView: View {
    @State private var watchlist: [String] = []
    @State private var newSymbol = ""
    @State private var isSaving = false
    @State private var isLoading = true

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Watchlist")
                .font(.headline)

            if isLoading {
                ProgressView().frame(maxWidth: .infinity)
            } else {
                FlowLayout(items: watchlist) { symbol in
                    HStack(spacing: 4) {
                        Text(symbol)
                            .font(.subheadline)
                            .fontWeight(.medium)
                        Button {
                            watchlist.removeAll { $0 == symbol }
                            saveWatchlist()
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(Color(.systemBackground))
                    .clipShape(Capsule())
                }
            }

            HStack {
                TextField("Add symbol (e.g. AMZN)", text: $newSymbol)
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
                    .padding(8)
                    .background(Color(.systemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 8))

                Button("Add") {
                    let sym = newSymbol.uppercased().trimmingCharacters(in: .whitespaces)
                    if !sym.isEmpty && !watchlist.contains(sym) {
                        watchlist.append(sym)
                        saveWatchlist()
                    }
                    newSymbol = ""
                }
                .buttonStyle(.borderedProminent)
                .disabled(newSymbol.isEmpty || isSaving)
            }
        }
        .padding()
        .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
        .task { await loadWatchlist() }
    }

    private func loadWatchlist() async {
        isLoading = true
        if let result = try? await APIService.shared.getWatchlist() {
            watchlist = result
        } else {
            watchlist = ["AAPL", "MSFT", "GOOGL", "TSLA", "SPY", "NVDA"]
        }
        isLoading = false
    }

    private func saveWatchlist() {
        isSaving = true
        let current = watchlist
        Task {
            try? await APIService.shared.setWatchlist(current)
            await MainActor.run { isSaving = false }
        }
    }
}

struct FlowLayout<Item: Hashable, Content: View>: View {
    let items: [Item]
    let content: (Item) -> Content

    init(items: [Item], @ViewBuilder content: @escaping (Item) -> Content) {
        self.items = items
        self.content = content
    }

    var body: some View {
        var width: CGFloat = 0
        var rows: [[Item]] = [[]]

        return GeometryReader { geo in
            ZStack(alignment: .topLeading) {
                ForEach(items, id: \.self) { item in
                    content(item)
                        .alignmentGuide(.leading) { d in
                            if width + d.width > geo.size.width {
                                width = 0
                                rows.append([])
                            }
                            let result = width
                            width += d.width + 8
                            return -result
                        }
                        .alignmentGuide(.top) { _ in
                            let row = rows.firstIndex(where: { $0.contains(item) }) ?? 0
                            return -CGFloat(row) * 36
                        }
                }
            }
        }
        .frame(height: CGFloat(max(1, (items.count + 3) / 4) * 40))
    }
}
