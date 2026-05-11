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

/// A simple wrapping chip layout. Measures each item width using a fixed
/// per-chip estimate, splits into rows, and renders with VStack+HStack.
/// This avoids the ZStack/alignmentGuide clipping bug in the GeometryReader approach.
struct FlowLayout<Item: Hashable, Content: View>: View {
    let items: [Item]
    let content: (Item) -> Content

    init(items: [Item], @ViewBuilder content: @escaping (Item) -> Content) {
        self.items = items
        self.content = content
    }

    var body: some View {
        GeometryReader { geo in
            let rows = computeRows(availableWidth: geo.size.width)
            VStack(alignment: .leading, spacing: 8) {
                ForEach(rows.indices, id: \.self) { rowIndex in
                    HStack(spacing: 8) {
                        ForEach(rows[rowIndex], id: \.self) { item in
                            content(item)
                        }
                    }
                }
            }
        }
        .frame(height: CGFloat(max(1, computeRowCount()) * 40))
    }

    private func chipWidth(for item: Item) -> CGFloat {
        // Approximate: 14px per char + 20px padding + 16px for the × button
        let str = "\(item)"
        return CGFloat(str.count) * 10 + 36
    }

    private func computeRowCount() -> Int {
        // Estimate with a 300pt width
        computeRows(availableWidth: 300).count
    }

    private func computeRows(availableWidth: CGFloat) -> [[Item]] {
        var rows: [[Item]] = [[]]
        var rowWidth: CGFloat = 0
        for item in items {
            let w = chipWidth(for: item) + 8 // +8 for spacing
            if rowWidth + w > availableWidth && !rows[rows.count - 1].isEmpty {
                rows.append([])
                rowWidth = 0
            }
            rows[rows.count - 1].append(item)
            rowWidth += w
        }
        return rows
    }
}
