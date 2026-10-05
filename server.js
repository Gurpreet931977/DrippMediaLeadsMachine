/**
 * Dripp Media - No Website Lead Engine
 * Local Node.js API / Backend Integration Layer
 * 
 * Architecture:
 *   Browser UI (localhost)
 *     ↓
 *   Local Node.js API / Backend (server.js)
 *     ↓
 *   Apify API & Google Sheets API (Credentials kept server-side via .env)
 */

const express = require('express');
const cors = require('cors');
const path = require('path');
const { spawn, execFile } = require('child_process');
require('dotenv').config();

const app = express();
const PORT = process.env.PORT || 8000;
const VENV_PYTHON = path.join(__dirname, '.venv', 'bin', 'python');

app.use(cors());
app.use(express.json());
app.use('/static', express.static(path.join(__dirname, 'static')));
app.use('/Logos', express.static(path.join(__dirname, 'Logos')));
app.use('/logos', express.static(path.join(__dirname, 'Logos')));
app.get('/favicon.ico', (req, res) => res.sendFile(path.join(__dirname, 'static', 'logos', 'favicon.png')));
app.get('/favicon.png', (req, res) => res.sendFile(path.join(__dirname, 'static', 'logos', 'favicon.png')));

// Global state for live pipeline execution & stats
const activeRunState = {
  is_running: false,
  progress_logs: [],
  current_stats: {
    requested_qualified_leads: 10,
    businesses_researched: 0,
    country_matches: 0,
    country_mismatches: 0,
    country_unclear: 0,
    website_exists: 0,
    no_website_confirmed: 0,
    website_unclear: 0,
    website_broken: 0,
    not_qualified: 0,
    duplicates: 0,
    qualified_leads: 0,
    outreach_ready: 0,
    manual_review: 0,
    research_only: 0,
    excluded: 0,
    high_priority: 0,
    medium_priority: 0,
    low_priority: 0,
    saved_to_leads: 0,
    saved_to_review_queue: 0,
    saved_to_research_log: 0
  },
  last_error: null
};

// Cache for Google Sheets data to enable instant UI tab switching
let leadsCache = null;
let reviewQueueCache = null;
let researchLogCache = null;
let lastLeadsFetchTime = 0;
let lastReviewQueueFetchTime = 0;
let lastResearchFetchTime = 0;
const CACHE_TTL_MS = 15000;

// Serve main web dashboard
app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'static', 'index.html'));
});

// GET /api/status - Current status & stats
app.get('/api/status', (req, res) => {
  res.json(activeRunState);
});

// SSE Streaming: GET /api/stream
app.get('/api/stream', (req, res) => {
  res.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    'Connection': 'keep-alive',
    'Access-Control-Allow-Origin': '*'
  });

  let lastIndex = 0;
  const timer = setInterval(() => {
    const logs = activeRunState.progress_logs;
    const newLogs = logs.slice(lastIndex);
    lastIndex = logs.length;

    const payload = {
      is_running: activeRunState.is_running,
      stats: activeRunState.current_stats,
      logs: newLogs
    };

    res.write(`data: ${JSON.stringify(payload)}\n\n`);

    if (!activeRunState.is_running && newLogs.length === 0 && lastIndex > 0) {
      // Keep connection open or allow client to poll
    }
  }, 1000);

  req.on('close', () => {
    clearInterval(timer);
  });
});

// POST /api/search - Trigger Lead Discovery & Qualification Pipeline
app.post('/api/search', (req, res) => {
  if (activeRunState.is_running) {
    return res.status(409).json({
      status: 'error',
      message: 'A lead generation task is already in progress.'
    });
  }

  const {
    country = 'United Kingdom',
    cities = ['Manchester'],
    industry = 'Restaurants',
    limit = 10,
    batch_size = 10,
    max_research_multiplier = 5
  } = req.body;

  const qualifiedNeeded = parseInt(limit, 10) || 10;
  const cityStr = Array.isArray(cities) ? cities.join(',') : (cities || 'Manchester');

  activeRunState.is_running = true;
  activeRunState.progress_logs = [];
  activeRunState.last_error = null;
  activeRunState.current_stats = {
    requested_qualified_leads: qualifiedNeeded,
    businesses_researched: 0,
    country_matches: 0,
    country_mismatches: 0,
    country_unclear: 0,
    website_exists: 0,
    no_website_confirmed: 0,
    website_unclear: 0,
    website_broken: 0,
    not_qualified: 0,
    duplicates: 0,
    qualified_leads: 0,
    outreach_ready: 0,
    manual_review: 0,
    research_only: 0,
    excluded: 0,
    high_priority: 0,
    medium_priority: 0,
    low_priority: 0,
    saved_to_leads: 0,
    saved_to_review_queue: 0,
    saved_to_research_log: 0
  };

  // Invalidate caches
  leadsCache = null;
  reviewQueueCache = null;
  researchLogCache = null;

  const args = [
    path.join(__dirname, 'run_pipeline.py'),
    '--country', country,
    '--city', cityStr,
    '--industry', industry,
    '--limit', qualifiedNeeded.toString(),
    '--batch-size', batch_size.toString(),
    '--multiplier', max_research_multiplier.toString()
  ];

  console.log(`[NodeServer] Spawning pipeline: ${VENV_PYTHON} ${args.join(' ')}`);
  const child = spawn(VENV_PYTHON, args, {
    cwd: __dirname,
    env: { ...process.env, PYTHONUNBUFFERED: '1' }
  });

  const appendLog = (line) => {
    if (!line.trim()) return;
    activeRunState.progress_logs.push(line);
    if (activeRunState.progress_logs.length > 500) {
      activeRunState.progress_logs.shift();
    }

    // Real-time metric extraction from log stream
    const stats = activeRunState.current_stats;
    const researchedMatch = line.match(/(?:\[Candidate\]|\u25B6|Candidate:)\s*\[?(\d+)\/(\d+)\]?/i) || line.match(/\[(\d+)\/(\d+)\]\s*Candidate:/i);
    if (researchedMatch) stats.businesses_researched = parseInt(researchedMatch[1], 10);

    if (line.includes('Country Mismatch')) stats.country_mismatches += 1;
    if (line.includes('Website Exists')) stats.website_exists += 1;
    if (line.includes('Website Broken')) stats.website_broken += 1;
    if (line.includes('Website Unclear')) stats.website_unclear += 1;
    if (line.includes('Confirmed No Website')) stats.no_website_confirmed += 1;
    if (line.includes('Not Qualified')) stats.not_qualified += 1;
    if (line.includes('QUALIFIED LEAD') || line.includes('OUTREACH_READY') || line.includes('OUTREACH READY')) {
      stats.qualified_leads += 1;
      stats.outreach_ready += 1;
    }
    if (line.includes('Manual Review') || line.includes('MANUAL_REVIEW')) stats.manual_review += 1;
    if (line.includes('Research Only') || line.includes('RESEARCH_ONLY')) stats.research_only += 1;
    if (line.includes('HIGH Priority')) stats.high_priority += 1;
    if (line.includes('MEDIUM Priority')) stats.medium_priority += 1;
    if (line.includes('LOW Priority')) stats.low_priority += 1;

    // Report block matches
    const repMatch = line.match(/^([A-Za-z\s-]+):\s*(\d+)$/);
    if (repMatch) {
      const key = repMatch[1].trim().toLowerCase();
      const val = parseInt(repMatch[2], 10);
      if (key === 'businesses researched' || key === 'valid candidates researched' || key === 'candidates researched') {
        stats.businesses_researched = val;
        stats.valid_candidates_researched = val;
      }
      if (key === 'country matches' || key === 'target-country matches') stats.country_matches = val;
      if (key === 'country mismatches' || key === 'wrong-country results') stats.country_mismatches = val;
      if (key === 'raw businesses discovered') stats.raw_discovered = val;
      if (key === 'website exists') stats.website_exists = val;
      if (key === 'no website confirmed') stats.no_website_confirmed = val;
      if (key === 'website unclear') stats.website_unclear = val;
      if (key === 'website broken') stats.website_broken = val;
      if (key === 'not qualified') stats.not_qualified = val;
      if (key === 'duplicates') stats.duplicates = val;
      if (key === 'outreach-ready' || key === 'outreach ready' || key === 'actual outreach-ready leads') {
        stats.outreach_ready = val;
        stats.qualified_leads = val;
      }
      if (key === 'manual review') stats.manual_review = val;
      if (key === 'research only') stats.research_only = val;
      if (key === 'excluded') stats.excluded = val;
      if (key === 'high priority') stats.high_priority = val;
      if (key === 'medium priority') stats.medium_priority = val;
      if (key === 'low priority') stats.low_priority = val;
      if (key === 'saved to leads') stats.saved_to_leads = val;
      if (key === 'saved to review_queue' || key === 'saved to review queue') stats.saved_to_review_queue = val;
      if (key === 'saved to research_log') stats.saved_to_research_log = val;
    }
  };

  child.stdout.on('data', (chunk) => {
    const lines = chunk.toString().split('\n');
    for (const l of lines) {
      const clean = l.replace(/\u001b\[[0-9;]*m/g, '');
      appendLog(clean);
    }
  });

  child.stderr.on('data', (chunk) => {
    const lines = chunk.toString().split('\n');
    for (const l of lines) {
      if (!l.trim()) continue;
      const clean = l.replace(/\u001b\[[0-9;]*m/g, '').trim();

      // Suppress internal crawler worker retries for places outside target area
      if (
        clean.includes('HttpCrawler: Reclaiming failed request') ||
        clean.includes('checkForUnrelatedPlaces') ||
        clean.includes('retryHistogram') ||
        clean.includes('INPUT DEPRECATION') ||
        clean.includes('CheerioCrawler: Using the old RequestQueue') ||
        clean.includes('Google redirect url stats') ||
        clean.includes('Geolocation') ||
        clean.includes('[BG ENQUEUE]')
      ) {
        continue;
      }

      // Format helpful Apify cloud crawler progress updates
      if (clean.includes('Starting container') || clean.includes('Creating container') || clean.includes('Pulling container')) {
        appendLog(`[Apify] [INIT] Initializing cloud scraper container...`);
        continue;
      }
      if (clean.includes('Starting the crawler')) {
        appendLog(`[Apify] [CRAWLER] Cloud crawler active. Searching Google Maps...`);
        continue;
      }
      const placesMatch = clean.match(/(?:\uD83D\uDCCA|\b)\s*(\d+)\s*places scraped/);
      if (placesMatch) {
        appendLog(`[Apify] [EXTRACTED] Scraped ${placesMatch[1]} candidate places.`);
        continue;
      }
      if (clean.includes('Finished! Total') && clean.includes('succeeded')) {
        appendLog(`[Apify] [OK] Cloud scraping run completed successfully.`);
        continue;
      }

      // Only flag genuine unhandled Python exceptions or fatal errors as [ERROR]
      if (/(?:Traceback \(most recent call last\)|(?<!\w)Exception:|(?<!\w)Error:|\bfatal\b)/i.test(clean) && !clean.includes('[apify.')) {
        appendLog(`[ERROR] ${clean}`);
      } else if (!clean.includes('[apify.') && !clean.includes('runId:')) {
        appendLog(clean);
      }
    }
  });

  child.on('close', (code) => {
    activeRunState.is_running = false;
    console.log(`[NodeServer] Pipeline process exited with code ${code}`);
    if (code !== 0) {
      activeRunState.last_error = `Process exited with code ${code}`;
    }
  });

  child.on('error', (err) => {
    activeRunState.is_running = false;
    activeRunState.last_error = err.message;
    console.error(`[NodeServer] Failed to spawn child process:`, err);
  });

  return res.json({
    status: 'started',
    message: `Lead engine started. Researching candidates to find ${qualifiedNeeded} qualified leads.`
  });
});

// GET /api/leads - Fetch qualified leads securely from Google Sheets server-side
app.get('/api/leads', (req, res) => {
  const now = Date.now();
  if (leadsCache && (now - lastLeadsFetchTime < CACHE_TTL_MS)) {
    return res.json(leadsCache);
  }

  const scriptPath = path.join(__dirname, 'lib', 'sheets', 'fetch_data.py');
  execFile(VENV_PYTHON, [scriptPath, 'leads'], { cwd: __dirname, maxBuffer: 10 * 1024 * 1024 }, (error, stdout, stderr) => {
    if (error) {
      console.error('[NodeServer] Error fetching leads from Google Sheets:', stderr || error.message);
      return res.status(500).json({ status: 'error', message: stderr || error.message });
    }
    try {
      const jsonStart = stdout.indexOf('{');
      const jsonEnd = stdout.lastIndexOf('}');
      if (jsonStart === -1 || jsonEnd === -1) {
        throw new Error('No JSON object found in output');
      }
      const data = JSON.parse(stdout.slice(jsonStart, jsonEnd + 1));
      leadsCache = data;
      lastLeadsFetchTime = Date.now();
      res.json(data);
    } catch (e) {
      console.error('[NodeServer] JSON parse error in /api/leads:', e.message, stdout.substring(0, 300));
      res.status(500).json({ status: 'error', message: 'Failed to parse leads data.' });
    }
  });
});

// GET /api/review-queue - Fetch manual review queue securely from Google Sheets server-side
app.get('/api/review-queue', (req, res) => {
  const now = Date.now();
  if (reviewQueueCache && (now - lastReviewQueueFetchTime < CACHE_TTL_MS)) {
    return res.json(reviewQueueCache);
  }

  const scriptPath = path.join(__dirname, 'lib', 'sheets', 'fetch_data.py');
  execFile(VENV_PYTHON, [scriptPath, 'review_queue'], { cwd: __dirname, maxBuffer: 10 * 1024 * 1024 }, (error, stdout, stderr) => {
    if (error) {
      console.error('[NodeServer] Error fetching review queue from Google Sheets:', stderr || error.message);
      return res.status(500).json({ status: 'error', message: stderr || error.message });
    }
    try {
      const jsonStart = stdout.indexOf('{');
      const jsonEnd = stdout.lastIndexOf('}');
      if (jsonStart === -1 || jsonEnd === -1) {
        throw new Error('No JSON object found in output');
      }
      const data = JSON.parse(stdout.slice(jsonStart, jsonEnd + 1));
      reviewQueueCache = data;
      lastReviewQueueFetchTime = Date.now();
      res.json(data);
    } catch (e) {
      console.error('[NodeServer] JSON parse error in /api/review-queue:', e.message, stdout.substring(0, 300));
      res.status(500).json({ status: 'error', message: 'Failed to parse review queue data.' });
    }
  });
});

// GET /api/research-log - Fetch research entries securely from Google Sheets server-side
app.get('/api/research-log', (req, res) => {
  const now = Date.now();
  if (researchLogCache && (now - lastResearchFetchTime < CACHE_TTL_MS)) {
    return res.json(researchLogCache);
  }

  const scriptPath = path.join(__dirname, 'lib', 'sheets', 'fetch_data.py');
  execFile(VENV_PYTHON, [scriptPath, 'research_log'], { cwd: __dirname, maxBuffer: 10 * 1024 * 1024 }, (error, stdout, stderr) => {
    if (error) {
      console.error('[NodeServer] Error fetching research log:', stderr || error.message);
      return res.status(500).json({ status: 'error', message: stderr || error.message });
    }
    try {
      const jsonStart = stdout.indexOf('{');
      const jsonEnd = stdout.lastIndexOf('}');
      if (jsonStart === -1 || jsonEnd === -1) {
        throw new Error('No JSON object found in output');
      }
      const data = JSON.parse(stdout.slice(jsonStart, jsonEnd + 1));
      researchLogCache = data;
      lastResearchFetchTime = Date.now();
      res.json(data);
    } catch (e) {
      console.error('[NodeServer] JSON parse error in /api/research-log:', e.message, stdout.substring(0, 300));
      res.status(500).json({ status: 'error', message: 'Failed to parse research log data.' });
    }
  });
});

// ==========================================
// POST-PIPELINE OUTREACH & EXPERIMENT API
// ==========================================
const OUTREACH_CLI = path.join(__dirname, 'lib', 'outreach', 'outreach_cli.py');

function runOutreachCli(action, args = [], inputPayload = null) {
  return new Promise((resolve, reject) => {
    const proc = spawn(VENV_PYTHON, [OUTREACH_CLI, action, ...args], { cwd: __dirname });
    let stdoutData = '';
    let stderrData = '';

    if (inputPayload) {
      proc.stdin.write(typeof inputPayload === 'string' ? inputPayload : JSON.stringify(inputPayload));
      proc.stdin.end();
    }

    proc.stdout.on('data', (d) => { stdoutData += d.toString(); });
    proc.stderr.on('data', (d) => { stderrData += d.toString(); });

    proc.on('close', (code) => {
      try {
        const jsonStart = stdoutData.indexOf('{');
        const arrStart = stdoutData.indexOf('[');
        const start = (jsonStart !== -1 && arrStart !== -1) ? Math.min(jsonStart, arrStart) : (jsonStart !== -1 ? jsonStart : arrStart);
        if (start === -1) {
          if (code !== 0) return reject(new Error(stderrData || `CLI exited with code ${code}`));
          throw new Error('No JSON output returned');
        }
        const jsonStr = stdoutData.slice(start);
        resolve(JSON.parse(jsonStr));
      } catch (err) {
        reject(new Error(`Failed to parse CLI output: ${err.message}`));
      }
    });

    proc.on('error', (err) => reject(err));
  });
}

// GET /api/channels - Status of sending providers (Section 23: Channel Guard)
app.get('/api/channels', async (req, res) => {
  try {
    const channels = await runOutreachCli('get_channels');
    res.json({ status: 'ok', channels });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/campaigns - List all outreach campaigns
app.get('/api/campaigns', async (req, res) => {
  try {
    const campaigns = await runOutreachCli('list_campaigns');
    res.json({ status: 'ok', campaigns });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/campaigns - Create a new outreach campaign
app.post('/api/campaigns', async (req, res) => {
  try {
    const campaign = await runOutreachCli('create_campaign', [], req.body);
    res.json({ status: 'ok', campaign });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/outreach/activation-queue - List qualified lead activation queue (Phase 9.3)
app.get('/api/outreach/activation-queue', async (req, res) => {
  try {
    const runPath = path.join(__dirname, 'data', 'phase_9_3_contactability_run.json');
    if (fs.existsSync(runPath)) {
      const data = JSON.parse(fs.readFileSync(runPath, 'utf-8'));
      let queue = data.ACTIVATION_QUEUE || [];
      if (!req.query.include_all) {
        queue = queue.filter(p => p.qualification_state === 'OUTREACH_READY');
      }
      return res.json({
        status: 'ok',
        run_id: data.RUN_ID,
        total_outreach_ready: data.INPUT_OUTREACH_READY,
        activation_ready_count: data.ACTIVATION_READY,
        activation_blocked_count: data.ACTIVATION_BLOCKED,
        queue: queue
      });
    }
    res.status(404).json({ status: 'error', message: 'Phase 9.3 run output not found. Please run scripts/run_phase_9_3_contactability.py' });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/outreach/operator-preview/:lead_id - Pre-contact inspection screen (Phase 9.4)
app.get('/api/outreach/operator-preview/:lead_id', async (req, res) => {
  try {
    const leadId = req.params.lead_id;
    const pyScript = `
import json, sys
from lib.outreach.phase_9_4_operator_gateway import OperatorSendGateway
gateway = OperatorSendGateway()
try:
    preview = gateway.generate_operator_preview('${leadId}')
    print(json.dumps({'status': 'ok', 'preview': preview}))
except Exception as e:
    print(json.dumps({'status': 'error', 'message': str(e)}))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) {
        return res.status(500).json({ status: 'error', message: stderr || err.message });
      }
      try {
        const parsed = JSON.parse(stdout.trim());
        if (parsed.status === 'error') {
          return res.status(404).json(parsed);
        }
        res.json(parsed);
      } catch (parseErr) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/outreach/operator-action/call - Manual phone outreach action (Phase 9.4)
app.post('/api/outreach/operator-action/call', async (req, res) => {
  try {
    const { lead_id, operator_confirmed, outcome, notes, test_mode } = req.body;
    const pyScript = `
import json, sys
from lib.outreach.phase_9_4_operator_gateway import OperatorSendGateway
gateway = OperatorSendGateway()
res = gateway.execute_manual_phone_action(
    lead_id=${JSON.stringify(lead_id)},
    operator_confirmed=${operator_confirmed ? 'True' : 'False'},
    outcome=${JSON.stringify(outcome || '')},
    notes=${JSON.stringify(notes || '')},
    test_mode=${test_mode ? 'True' : 'False'}
)
print(json.dumps(res))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.success) return res.status(400).json(parsed);
        res.json(parsed);
      } catch (parseErr) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/outreach/operator-action/social - Manual social outreach actions (Phase 9.4)
app.post('/api/outreach/operator-action/social', async (req, res) => {
  try {
    const { lead_id, channel, action, operator_confirmed, notes, test_mode } = req.body;
    const pyScript = `
import json, sys
from lib.outreach.phase_9_4_operator_gateway import OperatorSendGateway
gateway = OperatorSendGateway()
res = gateway.execute_manual_social_action(
    lead_id=${JSON.stringify(lead_id)},
    channel=${JSON.stringify(channel || 'INSTAGRAM')},
    action=${JSON.stringify(action || '')},
    operator_confirmed=${operator_confirmed ? 'True' : 'False'},
    notes=${JSON.stringify(notes || '')},
    test_mode=${test_mode ? 'True' : 'False'}
)
print(json.dumps(res))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.success) return res.status(400).json(parsed);
        res.json(parsed);
      } catch (parseErr) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/outreach/operator-action/response - Record response (Phase 9.4)
app.post('/api/outreach/operator-action/response', async (req, res) => {
  try {
    const { lead_id, response_type, notes, evidence, test_mode } = req.body;
    const pyScript = `
import json, sys
from lib.outreach.phase_9_4_operator_gateway import OperatorSendGateway
gateway = OperatorSendGateway()
res = gateway.record_manual_response(
    lead_id=${JSON.stringify(lead_id)},
    response_type=${JSON.stringify(response_type || '')},
    notes=${JSON.stringify(notes || '')},
    evidence=${JSON.stringify(evidence || '')},
    test_mode=${test_mode ? 'True' : 'False'}
)
print(json.dumps(res))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.success) return res.status(400).json(parsed);
        res.json(parsed);
      } catch (parseErr) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// =========================================================================
// Phase 9.5 Controlled Outreach Batch & Real Outcome Analytics Endpoints
// =========================================================================

// GET /api/outreach/controlled-batch-analytics
app.get('/api/outreach/controlled-batch-analytics', async (req, res) => {
  try {
    const pyScript = `
import json, sys
from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
executor = ControlledBatchExecutor()
print(json.dumps({'status': 'ok', 'analytics': executor.get_controlled_batch_analytics()}))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        res.json(JSON.parse(stdout.trim()));
      } catch (e) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/outreach/controlled-batch
app.get('/api/outreach/controlled-batch', async (req, res) => {
  try {
    const pyScript = `
import json, sys
from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
executor = ControlledBatchExecutor()
current = executor.get_current_batch_lead()
if 'error' in current and current['error'] == 'NO_ACTIVE_BATCH':
    executor.init_batch()
    current = executor.get_current_batch_lead()
print(json.dumps({'status': 'ok', 'batch': current}))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        res.json(JSON.parse(stdout.trim()));
      } catch (e) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/outreach/controlled-batch/preview
app.post('/api/outreach/controlled-batch/preview', async (req, res) => {
  try {
    const pyScript = `
import json, sys
from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
executor = ControlledBatchExecutor()
res = executor.preview_current_lead()
print(json.dumps(res))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.success) return res.status(400).json(parsed);
        res.json({ status: 'ok', ...parsed });
      } catch (e) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/outreach/controlled-batch/action
app.post('/api/outreach/controlled-batch/action', async (req, res) => {
  try {
    const { operator_confirmed, action, notes, test_mode } = req.body;
    const pyScript = `
import json, sys
from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
executor = ControlledBatchExecutor()
res = executor.execute_current_lead_action(
    operator_confirmed=${operator_confirmed ? 'True' : 'False'},
    action=${JSON.stringify(action || 'CALL')},
    notes=${JSON.stringify(notes || '')},
    test_mode=${test_mode ? 'True' : 'False'}
)
print(json.dumps(res))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.success) return res.status(400).json(parsed);
        res.json({ status: 'ok', ...parsed });
      } catch (e) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/outreach/controlled-batch/outcome
app.post('/api/outreach/controlled-batch/outcome', async (req, res) => {
  try {
    const { outcome, notes, callback_time, test_mode } = req.body;
    const pyScript = `
import json, sys
from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
executor = ControlledBatchExecutor()
res = executor.record_current_lead_outcome(
    outcome=${JSON.stringify(outcome || '')},
    notes=${JSON.stringify(notes || '')},
    callback_time=${callback_time ? JSON.stringify(callback_time) : 'None'},
    test_mode=${test_mode ? 'True' : 'False'}
)
print(json.dumps(res))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.success) return res.status(400).json(parsed);
        res.json({ status: 'ok', ...parsed });
      } catch (e) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/outreach/controlled-batch/next
app.post('/api/outreach/controlled-batch/next', async (req, res) => {
  try {
    const pyScript = `
import json, sys
from lib.outreach.controlled_batch_executor import ControlledBatchExecutor
executor = ControlledBatchExecutor()
res = executor.next_lead()
print(json.dumps(res))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        const parsed = JSON.parse(stdout.trim());
        if (!parsed.success) return res.status(400).json(parsed);
        res.json({ status: 'ok', ...parsed });
      } catch (e) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/outreach/performance - Performance Intelligence Dashboard (Phase 9.6)
app.get('/api/outreach/performance', async (req, res) => {
  try {
    const pyScript = `
import json, sys
from lib.analytics.outreach_performance_engine import OutreachPerformanceEngine
engine = OutreachPerformanceEngine()
perf = engine.calculate_outreach_performance()
print(json.dumps(perf))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        const parsed = JSON.parse(stdout.trim());
        res.json({ status: 'ok', ...parsed });
      } catch (e) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/outreach/next-best-leads - Ranked Qualified Leads (Phase 9.6)
app.get('/api/outreach/next-best-leads', async (req, res) => {
  try {
    const pyScript = `
import json, sys
from lib.analytics.outreach_performance_engine import OutreachPerformanceEngine
engine = OutreachPerformanceEngine()
leads = engine.get_next_best_leads()
queue = engine.get_next_batch_recommendations()
print(json.dumps({"count": len(leads), "leads": leads, "queue_recommendation": queue}))
`;
    execFile(VENV_PYTHON, ['-c', pyScript], { cwd: __dirname }, (err, stdout, stderr) => {
      if (err) return res.status(500).json({ status: 'error', message: stderr || err.message });
      try {
        const parsed = JSON.parse(stdout.trim());
        res.json({ status: 'ok', ...parsed });
      } catch (e) {
        res.status(500).json({ status: 'error', message: stdout });
      }
    });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/outreach-queue - List persistent queue items
app.get('/api/outreach-queue', async (req, res) => {
  try {
    const campaign_id = req.query.campaign_id;
    const args = campaign_id ? [campaign_id] : [];
    const queue = await runOutreachCli('get_queue', args);
    res.json({ status: 'ok', queue });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

/**
 * Enriches a list of lead_ids into full lead objects by looking them up in the
 * cached/fresh Sheets leads data. This is required because the frontend only sends
 * lead_ids; the Python CLI needs full lead data for eligibility checking and message generation.
 */
async function enrichLeadIds(leadIds) {
  // Use cached leads or fetch fresh
  let leadsData = [];
  if (leadsCache && leadsCache.leads) {
    leadsData = leadsCache.leads;
  } else {
    const scriptPath = path.join(__dirname, 'lib', 'sheets', 'fetch_data.py');
    try {
      const stdout = await new Promise((resolve, reject) => {
        execFile(VENV_PYTHON, [scriptPath, 'leads'], { cwd: __dirname, maxBuffer: 5 * 1024 * 1024 }, (err, out) => {
          if (err) return reject(err);
          resolve(out);
        });
      });
      const s = stdout.indexOf('{');
      const e = stdout.lastIndexOf('}');
      if (s !== -1 && e !== -1) {
        const parsed = JSON.parse(stdout.slice(s, e + 1));
        leadsData = parsed.leads || [];
        leadsCache = parsed;
        lastLeadsFetchTime = Date.now();
      }
    } catch (e) {
      console.error('[NodeServer] Failed to enrich lead_ids from Sheets:', e.message);
    }
  }
  const idSet = new Set(leadIds);
  return leadsData.filter(l => idSet.has(l.lead_id));
}

// POST /api/outreach-queue/add
// Queue selected AUTO leads (re-checks eligibility, duplicate protection).
// Accepts { lead_ids: [...], campaign_name, channel, daily_limit, ... }
// Server enriches lead_ids → full lead objects before passing to Python CLI.
app.post('/api/outreach-queue/add', async (req, res) => {
  try {
    const { lead_ids, leads, ...rest } = req.body;
    let enrichedLeads = leads || [];
    if ((!leads || leads.length === 0) && lead_ids && lead_ids.length > 0) {
      enrichedLeads = await enrichLeadIds(lead_ids);
    }
    const payload = { ...rest, leads: enrichedLeads };
    const result = await runOutreachCli('queue_leads', [], payload);
    leadsCache = null; // invalidate so fresh outreach state loads on next fetch
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/outreach-queue/dry-run  (Section 25)
// Preview validation: generates messages, checks eligibility, shows WHAT WOULD be sent.
// Does NOT persist queue items. Does NOT contact any business.
// outreach_status = READY_FOR_SEND — never SENT.
app.post('/api/outreach-queue/dry-run', async (req, res) => {
  try {
    const { lead_ids, leads, ...rest } = req.body;
    let enrichedLeads = leads || [];
    if ((!leads || leads.length === 0) && lead_ids && lead_ids.length > 0) {
      enrichedLeads = await enrichLeadIds(lead_ids);
    }
    const payload = { ...rest, leads: enrichedLeads };
    const result = await runOutreachCli('dry_run', [], payload);
    // No cache invalidation — dry run writes nothing
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/manual-outreach/assign  (Section 19)
// Assigns selected leads to MANUAL mode: outreach_mode=MANUAL, outreach_status=READY_FOR_REVIEW.
// Does NOT log a call — that is a separate action via /api/manual-outreach/log.
app.post('/api/manual-outreach/assign', async (req, res) => {
  try {
    const { lead_ids, leads, ...rest } = req.body;
    let enrichedLeads = leads || [];
    if ((!leads || leads.length === 0) && lead_ids && lead_ids.length > 0) {
      enrichedLeads = await enrichLeadIds(lead_ids);
    }
    const payload = { ...rest, leads: enrichedLeads };
    const result = await runOutreachCli('assign_manual', [], payload);
    leadsCache = null;
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/manual-outreach/log - Log a manual call, notes, and outcome for a specific lead
app.post('/api/manual-outreach/log', async (req, res) => {
  try {
    const result = await runOutreachCli('log_call', [], req.body);
    leadsCache = null;
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/manual-outreach/logs?lead_id=...  — retrieve logged calls for a lead
app.get('/api/manual-outreach/logs', async (req, res) => {
  try {
    const lead_id = req.query.lead_id;
    const args = lead_id ? [lead_id] : [];
    const result = await runOutreachCli('get_manual_logs', args);
    res.json({ status: 'ok', logs: result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/experiments - Comparative experiment metrics: Manual vs Auto (Section 20)
app.get('/api/experiments', async (req, res) => {
  try {
    const experiments = await runOutreachCli('get_experiments');
    res.json({ status: 'ok', experiments });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/outreach/channel-router
// Evaluates all channels for a set of leads without routing or queuing.
// Returns per-lead channel eligibility matrix + aggregate availability summary.
// Used by UI to show Channel Availability overview and lead-by-lead breakdown.
app.post('/api/outreach/channel-router', async (req, res) => {
  try {
    const { lead_ids, leads, ...rest } = req.body;
    let enrichedLeads = leads || [];
    if ((!leads || leads.length === 0) && lead_ids && lead_ids.length > 0) {
      enrichedLeads = await enrichLeadIds(lead_ids);
    }
    const payload = { ...rest, leads: enrichedLeads };
    const result = await runOutreachCli('get_channel_router', [], payload);
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/leads/assess-contactability
// Evaluates recipient verification, contactability, and UK PECR compliance for a single lead.
app.post('/api/leads/assess-contactability', async (req, res) => {
  try {
    const lead = req.body;
    const result = await runOutreachCli('assess_contactability', [], { lead });
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/leads/enrich-all
// Runs contact enrichment across all qualified leads and updates Google Sheets CRM.
app.post('/api/leads/enrich-all', async (req, res) => {
  try {
    const result = await runOutreachCli('enrich_qualified_leads', [], {});
    leadsCache = null; // Invalidate leads cache
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});


// ==========================================
// CAMPAIGN EXECUTION LAYER — HARDENED GATE
// Section 13 + Server-Side Confirmation
// ==========================================

/**
 * POST /api/campaigns/:id/arm
 * Arms a campaign for execution. Issues a server-side HMAC confirmation token.
 * The token is SINGLE-USE and expires in 300 seconds.
 * Only ARMED campaigns can be executed.
 * Transition: PREVIEWED → ARMED
 */
app.post('/api/campaigns/:id/arm', async (req, res) => {
  const campaign_id = req.params.id;
  try {
    const result = await runOutreachCli('arm_campaign', [], { campaign_id });
    if (result.ok === false || result.error) {
      return res.status(400).json({ status: 'error', message: result.error || 'Arm failed' });
    }
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

/**
 * POST /api/campaigns/:id/execute
 * Executes the campaign — ONLY if it is ARMED and the confirmation_token is valid.
 * Token is consumed immediately. Cannot re-execute without re-arming.
 * Section 13 hardening: Backend gate enforces:
 *   - ARMED state
 *   - token valid + not expired + not consumed
 *   - not already RUNNING or COMPLETED
 *   - 12-point per-item pre-send checks
 *   - idempotency + send locks
 */
app.post('/api/campaigns/:id/execute', async (req, res) => {
  const campaign_id = req.params.id;
  const { confirmation_token, max_per_run, delay_seconds } = req.body;

  // Server-side gate: confirmation_token REQUIRED
  if (!confirmation_token) {
    return res.status(400).json({
      status: 'error',
      message: 'GATE_REJECTED: confirmation_token is required. Arm the campaign first via POST /api/campaigns/:id/arm'
    });
  }

  try {
    const result = await runOutreachCli('execute_campaign', [], {
      campaign_id,
      confirmation_token,
      max_per_run: max_per_run || null,
      delay_seconds: delay_seconds != null ? delay_seconds : 1.5
    });
    leadsCache = null;
    if (result.error) {
      return res.status(400).json({ status: 'error', message: result.error, result });
    }
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

/**
 * POST /api/campaigns/:id/mark-previewed
 * Called automatically after a successful dry-run to transition state.
 * Transition: DRAFT → PREVIEWED
 */
app.post('/api/campaigns/:id/mark-previewed', async (req, res) => {
  const campaign_id = req.params.id;
  try {
    const result = await runOutreachCli('mark_previewed', [], { campaign_id });
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

/**
 * GET /api/campaigns/:id/gate-state
 * Returns the current execution_state of a campaign.
 */
app.get('/api/campaigns/:id/gate-state', async (req, res) => {
  const campaign_id = req.params.id;
  try {
    const result = await runOutreachCli('get_execution_gate_state', [campaign_id]);
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/campaigns/:id/status
app.get('/api/campaigns/:id/status', async (req, res) => {
  const campaign_id = req.params.id;
  try {
    const result = await runOutreachCli('get_execution_status', [campaign_id]);
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// GET /api/rate-limits
app.get('/api/rate-limits', async (req, res) => {
  try {
    const result = await runOutreachCli('get_rate_limits');
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});

// POST /api/outreach-queue/edit
app.post('/api/outreach-queue/edit', async (req, res) => {
  try {
    const result = await runOutreachCli('edit_queue_item', [], req.body);
    res.json({ status: 'ok', result });
  } catch (err) {
    res.status(500).json({ status: 'error', message: err.message });
  }
});


app.listen(PORT, '127.0.0.1', () => {
  console.log(`=======================================================`);
  console.log(`[DRIPP MEDIA] Node.js Integration Backend Running`);
  console.log(`[NETWORK] URL: http://127.0.0.1:${PORT}`);
  console.log(`[SECURITY] All API Credentials Stored Server-Side`);
  console.log(`=======================================================`);
});
