import { buildPrdView, renderPrdMarkdown } from './prd.model';

describe('renderPrdMarkdown', () => {
  it('renders a table whose last row lost its closing pipe instead of hanging', () => {
    const md = [
      '| Metric | Now | Target |',
      '|---|---|---|',
      '| Evening loss | 58,614 | lower',
      '',
      'Next paragraph.',
    ].join('\n');
    const html = renderPrdMarkdown(md);
    expect(html).toContain('<td>lower</td>');
    expect(html).toContain('<p>Next paragraph.</p>');
  });

  it('always makes progress on a line no block rule claims', () => {
    const html = renderPrdMarkdown('| lone pipe line without a header rule\n- item');
    expect(html).toContain('<li>item</li>');
  });
});

describe('buildPrdView title', () => {
  const finding = { rank: 1, origin: 'warehouse', stage: 'homecare', hypothesis: 'Most lost bookings abandon on the payment screen. More text.', confidence: 'high', confirm_via: '', evidence: [] };
  const base = { run_id: 49, status: 'completed', findings: [finding], code_gaps: [], prds: [] } as never;

  it('names the #1 PRD from its own finding on a non-pharmacy journey', () => {
    const view = buildPrdView({ ...(base as object), journey: 'homecare' } as never, 1);
    expect(view.title).toBe('Fix: Most lost bookings abandon on the payment screen');
  });

  it('keeps the curated pharmacy name for the pharmacy demo', () => {
    const view = buildPrdView({ ...(base as object), journey: 'pd_checkout' } as never, 1);
    expect(view.title).toBe('Pharmacy Checkout Rescue');
  });
});
