import { Component, computed, effect, inject, input, output, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';

import { buildPrdView, renderPrdMarkdown } from '../../../../core/models/prd.model';
import {
  PrdDoc, PrdSection, humanSectionCount, isEdited as isSectionDirty,
  newSection, parsePrd, serialisePrd,
} from '../../../../core/models/prd-doc';
import { PrdSummary, RunState } from '../../../../core/models/run-state';
import { DocxExportService } from '../../../../core/services/docx-export.service';
import { RunService } from '../../../../core/services/run.service';

interface ChatMessage {
  role: 'user' | 'assistant';
  text: string;
}

@Component({
  selector: 'app-prd-drawer',
  imports: [FormsModule],
  templateUrl: './prd-drawer.component.html',
  styleUrl: './prd-drawer.component.scss',
})
export class PrdDrawerComponent {
  private readonly runService = inject(RunService);
  private readonly docx = inject(DocxExportService);

  readonly run = input.required<RunState>();
  readonly open = input(false);
  readonly closed = output<void>();

  readonly toastMessage = signal<string | null>(null);
  private toastTimer: ReturnType<typeof setTimeout> | null = null;

  /** Which finding's PRD is on screen. Backend PRDs are keyed by finding
   *  rank (one per finding, up to MAX_PRDS_PER_RUN); the fixture has no
   *  `prds[]` yet, so ranks there fall back to one card per finding. */
  readonly selectedRank = signal(1);

  /**
   * With more than one PRD the drawer opens on a list of cards rather than a
   * strip of tabs: `#1 #2 #3` says nothing about what each document is, and
   * a run can draft up to five. Picking one switches to 'doc'; Back returns.
   * A single-PRD run never sees the list.
   */
  readonly view = signal<'list' | 'doc'>('list');

  readonly multiplePrds = computed(() => this.prdRanks().length > 1);

  /** The card's name. Prefer the artifact's own title, then the finding it
   *  was drafted for — never a bare rank, which is what the tabs showed. */
  titleFor(rank: number): string {
    const summary = this.run().prds.find((p) => p.finding_rank === rank);
    if (summary?.title) return summary.title;
    const finding = this.run().findings.find((f) => f.rank === rank);
    // Split on a sentence boundary, not on '.': hypotheses are full of
    // decimals ("converts at 0.3002 versus 0.3904"), and a bare split left
    // the card reading "…converts at 0".
    if (finding) return finding.hypothesis.split(/(?<=[.!?])\s+(?=[A-Z])/)[0].slice(0, 90);
    return `Finding #${rank}`;
  }

  openRank(rank: number): void {
    this.selectRank(rank);
    this.view.set('doc');
  }

  backToList(): void {
    this.view.set('list');
  }

  /**
   * Section bodies are markdown. Read mode renders it; edit mode shows the
   * source in a textarea. Rendering was lost when section editing landed —
   * the read branch printed the raw source, so `**bold**` and backticks
   * showed literally. renderPrdMarkdown escapes before it converts, so this
   * is safe to bind with [innerHTML].
   *
   * Rendered once per document rather than per template pass: bound as a
   * bare method it re-ran every regex in renderPrdMarkdown for every
   * section on each change-detection cycle, including on every keystroke
   * in the chat box.
   */
  private readonly sectionHtmlById = computed<Record<string, string>>(() => {
    const d = this.doc();
    if (!d) return {};
    const out: Record<string, string> = {};
    for (const section of d.sections) out[section.id] = renderPrdMarkdown(section.body);
    return out;
  });

  sectionHtml(id: string): string {
    return this.sectionHtmlById()[id] ?? '';
  }

  readonly prdRanks = computed<number[]>(() => {
    const run = this.run();
    if (run.prds.length) return [...run.prds].map((p) => p.finding_rank).sort((a, b) => a - b);
    return [...run.findings].map((f) => f.rank).sort((a, b) => a - b).slice(0, 5);
  });

  /** Real markdown artifact for the selected finding, if the backend wrote
   *  one — null in fixture mode or against an older backend, in which case
   *  the drawer falls back to the reconstructed structured view below. */
  readonly activeSummary = computed<PrdSummary | null>(() => {
    const rank = this.selectedRank();
    return this.run().prds.find((p) => p.finding_rank === rank) ?? null;
  });

  readonly markdownHtml = computed<string | null>(() => {
    const summary = this.activeSummary();
    return summary ? renderPrdMarkdown(summary.markdown) : null;
  });

  /** Structured fallback — always computed so the "Approve"/docx flows keep
   *  working even when a real markdown artifact is on screen. */
  readonly prd = computed(() => buildPrdView(this.run(), this.selectedRank()));

  /** Chat editing only makes sense against a real backend artifact: the
   *  endpoint reads/writes a RunArtifact row keyed by this run's id, which
   *  the frozen fixture (run 47) doesn't have. Gate on the live source
   *  rather than silently no-oping the button. */
  readonly chatAvailable = computed(() => this.runService.source() === 'live');

  private readonly chatLogs = signal<Record<number, ChatMessage[]>>({});
  readonly chatLog = computed<ChatMessage[]>(() => this.chatLogs()[this.selectedRank()] ?? []);
  readonly chatInput = signal('');
  readonly chatBusy = signal(false);

  /** Collapsed by default only once a conversation exists — an empty "Ask
   *  for a change" box is the drawer's one edit affordance and shouldn't
   *  start hidden, but a long chat log pushes the document itself out of
   *  view, so it's worth tucking away once there's something to tuck. */
  readonly chatSectionOpen = signal(true);
  toggleChatSection(): void {
    this.chatSectionOpen.update((open) => !open);
  }

  /**
   * Two edit modes, deliberately kept side by side rather than one replacing
   * the other:
   *
   *  - CHAT asks the backend to make a change and re-verify it
   *    (POST /runs/{id}/prd/{rank}/chat). Live runs only.
   *  - SECTION editing is direct manipulation of the markdown, in place.
   *    Works offline and on the fixture, but nothing verifies it.
   *
   * They complement each other: chat is better at "is this already built?",
   * section editing is better at "reword this and drop that". What they must
   * never do is look alike — a chat edit came back through the Remedy Loop,
   * a typed one did not, so typed sections stay badged as unverified.
   */
  readonly doc = signal<PrdDoc | null>(null);
  readonly editing = signal(false);
  readonly editedCount = computed(() => { const d = this.doc(); return d ? humanSectionCount(d) : 0; });

  constructor() {
    // Keep the selection valid as the run/prds change (e.g. fixture -> live).
    effect(() => {
      const ranks = this.prdRanks();
      if (ranks.length && !ranks.includes(this.selectedRank())) {
        this.selectedRank.set(ranks[0]);
      }
    });

    // Re-parse whenever the selected PRD changes — switching rank or a chat
    // edit landing means the sections on screen belong to a different
    // document, so in-progress local edits are dropped rather than silently
    // re-attached to other content.
    // The card list is the drawer's entry point when there is more than one
    // PRD, so opening it always lands there rather than resuming whichever
    // document was last read — which would also survive a switch to a
    // different run.
    effect(() => {
      if (this.open()) this.view.set('list');
    });

    effect(() => {
      const md = this.activeSummary()?.markdown ?? this.run().prd_draft;
      this.doc.set(md?.trim() ? parsePrd(md) : null);
      this.editing.set(false);
    });
  }

  toggleEdit(): void { this.editing.set(!this.editing()); }

  onSectionInput(section: PrdSection, body: string): void {
    this.doc.update((d) => d && ({
      ...d,
      sections: d.sections.map((s) =>
        s.id === section.id
          ? { ...s, body, origin: s.origin === 'agent' && body !== s.original ? ('human-edited' as const) : s.origin }
          : s),
    }));
  }

  onHeadingInput(section: PrdSection, heading: string): void {
    this.doc.update((d) => d && ({
      ...d,
      sections: d.sections.map((s) =>
        s.id === section.id ? { ...s, heading, origin: s.origin === 'agent' ? ('human-edited' as const) : s.origin } : s),
    }));
  }

  addSection(): void {
    this.doc.update((d) => d && ({ ...d, sections: [...d.sections, newSection()] }));
    this.editing.set(true);
  }

  removeSection(id: string): void {
    this.doc.update((d) => d && ({ ...d, sections: d.sections.filter((s) => s.id !== id) }));
  }

  move(id: string, delta: -1 | 1): void {
    this.doc.update((d) => {
      if (!d) return d;
      const i = d.sections.findIndex((s) => s.id === id);
      const j = i + delta;
      if (i < 0 || j < 0 || j >= d.sections.length) return d;
      const sections = [...d.sections];
      [sections[i], sections[j]] = [sections[j], sections[i]];
      return { ...d, sections };
    });
  }

  revertSection(id: string): void {
    this.doc.update((d) => d && ({
      ...d,
      sections: d.sections.map((s) =>
        s.id === id && s.original !== undefined ? { ...s, body: s.original, origin: 'agent' as const } : s),
    }));
  }

  revertAll(): void {
    const md = this.activeSummary()?.markdown ?? this.run().prd_draft;
    this.doc.set(md?.trim() ? parsePrd(md) : null);
    this.toast('Reverted to the generated PRD');
  }

  isSectionEdited(s: PrdSection): boolean { return isSectionDirty(s); }

  originLabel(s: PrdSection): string {
    return s.origin === 'human-added' ? 'ADDED BY YOU · UNVERIFIED' : 'EDITED BY YOU · UNVERIFIED';
  }

  /** The document as it stands, local edits included — what the .docx
   *  export writes and what a future POST /prd/revisions would send. */
  currentMarkdown(): string {
    const d = this.doc();
    return d ? serialisePrd(d) : (this.activeSummary()?.markdown ?? this.run().prd_draft ?? '');
  }

  selectRank(rank: number): void {
    this.selectedRank.set(rank);
  }

  isEdited(rank: number): boolean {
    return this.run().prds.find((p) => p.finding_rank === rank)?.edited ?? false;
  }

  close(): void {
    this.closed.emit();
  }

  async downloadDocx(): Promise<void> {
    await this.docx.download(this.run(), this.prd(), this.doc() ? this.currentMarkdown() : undefined);
    this.toast('Downloading ' + this.docxFilename());
  }

  private docxFilename(): string {
    return `CareLoop_PRD_run${this.run().run_id}_${this.prd().title.replace(/\W+/g, '_')}.docx`;
  }



  async sendChat(): Promise<void> {
    const message = this.chatInput().trim();
    if (!message || this.chatBusy() || !this.chatAvailable()) return;

    const rank = this.selectedRank();
    this.appendChat(rank, 'user', message);
    this.chatInput.set('');
    this.chatBusy.set(true);

    const res = await this.runService.chatOnPrd(this.run().run_id, rank, message);

    this.chatBusy.set(false);
    if ('error' in res) {
      this.appendChat(rank, 'assistant', `Couldn't reach the PRD editor: ${res.error}`);
      return;
    }
    this.appendChat(rank, 'assistant', res.reply);
  }

  private appendChat(rank: number, role: ChatMessage['role'], text: string): void {
    this.chatLogs.update((logs) => ({ ...logs, [rank]: [...(logs[rank] ?? []), { role, text }] }));
  }

  private toast(message: string): void {
    this.toastMessage.set(message);
    if (this.toastTimer) clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => this.toastMessage.set(null), 2600);
  }
}
