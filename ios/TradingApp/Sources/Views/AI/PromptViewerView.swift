import SwiftUI

struct PromptViewerView: View {
    @State private var promptData: PromptData? = nil
    @State private var overrideStatus: PromptOverrideStatus? = nil
    @State private var isLoading = true
    @State private var selectedTab = 0  // 0=Step1, 1=Step2
    @State private var overrideText: String = ""
    @State private var isSavingOverride = false
    @State private var overrideSaveSuccess = false
    @State private var overrideError: String? = nil

    private static let isoFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    private var savedAt: String {
        guard let raw = promptData?.saved_at,
              let date = Self.isoFormatter.date(from: raw) else { return "Unknown" }
        let formatter = DateFormatter()
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return formatter.string(from: date)
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {

                // ── Override Card ──
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 10) {
                        ZStack {
                            Circle().fill(Color.purple.opacity(0.12)).frame(width: 36, height: 36)
                            Image(systemName: "text.badge.plus")
                                .foregroundStyle(.purple).font(.system(size: 16))
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Prompt Override")
                                .font(.headline)
                            Text(overrideStatus?.active == true ? "Active — injected each cycle" : "Inactive — Claude uses default behaviour")
                                .font(.caption)
                                .foregroundStyle(overrideStatus?.active == true ? .purple : .secondary)
                        }
                        Spacer()
                        if isSavingOverride {
                            ProgressView().scaleEffect(0.8)
                        } else if overrideSaveSuccess {
                            Image(systemName: "checkmark.circle.fill")
                                .foregroundStyle(.green)
                                .transition(.scale.combined(with: .opacity))
                        }
                    }
                    .padding()

                    Divider().padding(.horizontal)

                    VStack(alignment: .leading, spacing: 8) {
                        Text("Extra instructions appended to every Claude prompt. Examples:")
                            .font(.caption).foregroundStyle(.secondary)
                        Text("\"Avoid tech stocks today\" · \"Focus on energy sector\" · \"Be conservative, only high-confidence trades\"")
                            .font(.caption2).foregroundStyle(.tertiary)
                            .italic()

                        TextEditor(text: $overrideText)
                            .font(.callout)
                            .frame(minHeight: 80, maxHeight: 150)
                            .padding(8)
                            .background(Color(.systemBackground))
                            .cornerRadius(10)
                            .overlay(
                                RoundedRectangle(cornerRadius: 10)
                                    .stroke(overrideStatus?.active == true ? Color.purple.opacity(0.4) : Color(.systemGray4), lineWidth: 1)
                            )

                        if let err = overrideError {
                            Text(err).font(.caption).foregroundStyle(.red)
                        }

                        HStack(spacing: 8) {
                            Button {
                                saveOverride()
                            } label: {
                                Label(overrideText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Clear Override" : "Apply Override",
                                      systemImage: overrideText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "xmark.circle" : "checkmark.circle")
                                    .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(overrideText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? .red : .purple)
                            .disabled(isSavingOverride)

                            if overrideStatus?.active == true {
                                Button {
                                    overrideText = ""
                                    saveOverride()
                                } label: {
                                    Label("Clear", systemImage: "trash")
                                }
                                .buttonStyle(.bordered)
                                .tint(.red)
                            }
                        }
                    }
                    .padding()
                }
                .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))

                // ── Prompt Viewer ──
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 10) {
                        ZStack {
                            Circle().fill(Color.blue.opacity(0.12)).frame(width: 36, height: 36)
                            Image(systemName: "doc.text.magnifyingglass")
                                .foregroundStyle(.blue).font(.system(size: 16))
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Last Prompt Sent to Claude")
                                .font(.headline)
                            if promptData?.available == true {
                                Text("Last cycle: \(savedAt)")
                                    .font(.caption).foregroundStyle(.secondary)
                            } else {
                                Text("No cycle run yet")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                    }
                    .padding()

                    if isLoading {
                        ProgressView().frame(maxWidth: .infinity).padding()
                    } else if promptData?.available != true {
                        Text(promptData?.message ?? "Waiting for first trading cycle.")
                            .font(.callout).foregroundStyle(.secondary)
                            .padding()
                    } else {
                        Divider().padding(.horizontal)

                        // Tab picker
                        Picker("Step", selection: $selectedTab) {
                            Text("Step 1 — Scan").tag(0)
                            Text("Step 2 — Decide").tag(1)
                        }
                        .pickerStyle(.segmented)
                        .padding()

                        let promptText = selectedTab == 0 ? (promptData?.step1 ?? "") : (promptData?.step2 ?? "")

                        ScrollView {
                            Text(promptText)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.primary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding()
                                .textSelection(.enabled)
                        }
                        .frame(maxHeight: 500)
                        .background(Color(.systemBackground))
                        .cornerRadius(10)
                        .padding(.horizontal)
                        .padding(.bottom)

                        // Copy button
                        Button {
                            UIPasteboard.general.string = promptText
                        } label: {
                            Label("Copy to Clipboard", systemImage: "doc.on.doc")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                        .padding([.horizontal, .bottom])
                    }
                }
                .background(RoundedRectangle(cornerRadius: 16).fill(Color(.systemGray6)))
            }
            .padding()
        }
        .navigationTitle("Claude Prompt")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
    }

    private func load() async {
        isLoading = true
        async let prompts = APIService.shared.getLastPrompts()
        async let override = APIService.shared.getPromptOverride()
        if let p = try? await prompts { promptData = p }
        if let o = try? await override {
            overrideStatus = o
            if overrideText.isEmpty {
                overrideText = o.override ?? ""
            }
        }
        isLoading = false
    }

    private func saveOverride() {
        isSavingOverride = true
        overrideError = nil
        let text = overrideText.trimmingCharacters(in: .whitespacesAndNewlines)
        Task {
            do {
                try await APIService.shared.setPromptOverride(text.isEmpty ? nil : text)
                if let o = try? await APIService.shared.getPromptOverride() {
                    await MainActor.run { overrideStatus = o }
                }
                await MainActor.run {
                    isSavingOverride = false
                    overrideSaveSuccess = true
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                await MainActor.run { overrideSaveSuccess = false }
            } catch {
                await MainActor.run {
                    isSavingOverride = false
                    overrideError = "Save failed: \(error.localizedDescription)"
                }
            }
        }
    }
}
