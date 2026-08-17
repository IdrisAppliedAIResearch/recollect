/**
 * A synthetic but plausible long-running conversation.
 *
 * Sixteen topics, so the deterministic 16-way clustering has something real
 * to find, and exchanges long enough that a 32,000-character budget is
 * genuinely scarce — which is the whole point of the mock. Text is composed
 * from per-topic word banks against shared sentence frames, so 120 episodes
 * come out distinct without 120 hand-written paragraphs.
 */

export interface Topic {
  key: string
  label: string
  subject: string
  nouns: string[]
  issues: string[]
  actions: string[]
  outcomes: string[]
  asks: string[]
}

export const TOPICS: Topic[] = [
  {
    key: 'sourdough',
    label: 'Sourdough',
    subject: 'the starter',
    nouns: ['the levain', 'the autolyse', 'the bulk ferment', 'the crumb', 'the scoring'],
    issues: ['a slack dough that will not hold shape', 'over-proofing in a warm kitchen', 'a gummy centre', 'a pale crust'],
    actions: ['drop the hydration by three points', 'shorten the bulk to four hours', 'bake covered for the first twenty minutes', 'feed at a 1:5:5 ratio'],
    outcomes: ['an open, even crumb', 'a crust that shatters', 'a loaf that holds its ear', 'a starter that doubles in five hours'],
    asks: [
      'my loaf came out dense again, what should I change',
      'how long should the bulk ferment run at 22C',
      'the starter smells like acetone by morning, is that bad',
      'can I retard the shaped loaf overnight and still get oven spring',
    ],
  },
  {
    key: 'marathon',
    label: 'Marathon',
    subject: 'the training block',
    nouns: ['the long run', 'threshold work', 'the taper', 'weekly volume', 'cadence'],
    issues: ['a niggle in the left achilles', 'heart rate drifting on easy days', 'stalling in the last 10k', 'poor sleep after evening sessions'],
    actions: ['cap easy runs at conversational pace', 'add one hill session a week', 'cut volume 30 percent in the taper', 'move the long run to the morning'],
    outcomes: ['a negative split', 'a repeatable threshold pace', 'legs that recover inside two days', 'a sub-3:30 finish'],
    asks: [
      'is 60k a week enough for a spring marathon',
      'my achilles is grumbling on the descents, should I stop',
      'how should I structure the last three weeks',
      'what pace should the long run actually be',
    ],
  },
  {
    key: 'rust',
    label: 'Rust',
    subject: 'the borrow checker',
    nouns: ['the lifetime annotation', 'an Arc<Mutex<_>>', 'the trait object', 'the iterator chain', 'a Cow<str>'],
    issues: ['a borrow that outlives its owner', 'an accidental clone in the hot loop', 'a Send bound the compiler cannot prove', 'a lifetime that infects the whole struct'],
    actions: ['take the value by reference instead of by move', 'split the struct so the borrows do not overlap', 'return an owned String at the boundary', 'hoist the allocation out of the loop'],
    outcomes: ['a compile with no clones', 'a signature that reads honestly', 'code that borrows for exactly as long as it needs', 'a hot path with no allocation'],
    asks: [
      'why does the compiler say this borrow lives too long',
      'should I reach for Rc or just clone the string here',
      'how do I return an iterator without boxing it',
      'is there a cheaper way to do this than a Mutex',
    ],
  },
  {
    key: 'kyoto',
    label: 'Kyoto trip',
    subject: 'the itinerary',
    nouns: ['the Arashiyama morning', 'the Keihan line', 'a ryokan booking', 'the temple pass', 'the Nishiki market walk'],
    issues: ['crowds after nine in the morning', 'a gap between the airport and check-in', 'too much backtracking across the city', 'a rainy afternoon with nothing indoors planned'],
    actions: ['front-load the eastern temples before eight', 'buy the day pass at the station rather than online', 'keep the second afternoon completely unplanned', 'book the ryokan dinner two months out'],
    outcomes: ['a first morning with the paths nearly empty', 'a route that never doubles back', 'an afternoon that survives rain', 'a trip with two genuinely slow days'],
    asks: [
      'how many days do I actually need in Kyoto',
      'is the JR pass worth it if I am staying put',
      'what is the least crowded way to see Fushimi Inari',
      'can we do a day trip to Nara without it feeling rushed',
    ],
  },
  {
    key: 'mortgage',
    label: 'Mortgage',
    subject: 'the refinance',
    nouns: ['the fixed period', 'the arrangement fee', 'the loan-to-value band', 'the early repayment charge', 'the offer expiry'],
    issues: ['a fee that eats the rate saving', 'slipping into a worse LTV band', 'an offer that expires before completion', 'an overpayment cap of ten percent'],
    actions: ['compare on total cost over the fixed period, not the headline rate', 'overpay to cross the 60 percent band before applying', 'lock the offer six months ahead', 'take the fee-free product at the higher rate'],
    outcomes: ['a lower total cost across five years', 'a band crossing worth more than the rate hunt', 'an offer held while the sale completes', 'no early repayment exposure'],
    asks: [
      'is it worth paying the fee to get the lower rate',
      'how far ahead can I lock a new deal',
      'does overpaying now actually help my LTV band',
      'two-year or five-year fix given where rates are',
    ],
  },
  {
    key: 'piano',
    label: 'Jazz piano',
    subject: 'the practice routine',
    nouns: ['rootless voicings', 'the ii-V-I', 'left-hand comping', 'the blues scale', 'a walking bass line'],
    issues: ['voicings that all sound the same', 'a right hand that runs out of ideas', 'time that drags in the turnaround', 'reading charts too slowly to play along'],
    actions: ['practise one voicing shape through all twelve keys', 'transcribe eight bars a week by ear', 'play with a metronome on beats two and four', 'limit yourself to three notes for a whole chorus'],
    outcomes: ['comping that breathes', 'lines that resolve where you meant them to', 'time that sits in the pocket', 'a chart you can read at tempo'],
    asks: [
      'what should I actually practise in twenty minutes a day',
      'how do I stop every solo sounding the same',
      'which tunes are worth learning first',
      'is transcribing by ear worth the time it takes',
    ],
  },
  {
    key: 'garden',
    label: 'Garden',
    subject: 'the vegetable beds',
    nouns: ['the tomato bed', 'the compost heap', 'the drip line', 'the brassica netting', 'the winter cover crop'],
    issues: ['blossom end rot on the first truss', 'slugs taking the seedlings overnight', 'a compost heap that never heats up', 'soil that dries out by midday'],
    actions: ['water evenly rather than heavily', 'sow the successions three weeks apart', 'turn the heap once a fortnight', 'mulch four inches deep before June'],
    outcomes: ['fruit that sets on every truss', 'seedlings that survive their first week', 'a heap that finishes in ten weeks', 'beds that hold moisture through August'],
    asks: [
      'why are my tomatoes rotting at the bottom',
      'when should I start the second sowing',
      'the compost is cold and slimy, what did I do wrong',
      'how deep should the mulch actually go',
    ],
  },
  {
    key: 'thesis',
    label: 'Thesis',
    subject: 'the urban heat chapter',
    nouns: ['the sensor network', 'the land-cover covariate', 'the mixed-effects model', 'the reviewer comments', 'the null result'],
    issues: ['a confound between canopy cover and income', 'sensors that drift over a season', 'a p-value that will not survive correction', 'a chapter that argues two things at once'],
    actions: ['report the effect size with its interval, not the test', 'recalibrate against the reference station monthly', 'pre-register the analysis before the summer data', 'split the chapter so each argument stands alone'],
    outcomes: ['an estimate a reviewer can check', 'a season of readings that stay comparable', 'a claim narrow enough to defend', 'a chapter with one thesis in it'],
    asks: [
      'how do I handle the canopy and income confound',
      'is the effect big enough to be worth reporting',
      'should I split this chapter in two',
      'what do I do about the sensor that drifted in July',
    ],
  },
  {
    key: 'mochi',
    label: 'Mochi the cat',
    subject: "Mochi's health",
    nouns: ['the renal panel', 'the wet-food switch', 'the water fountain', 'the annual bloodwork', 'the weight chart'],
    issues: ['creatinine creeping up year on year', 'a cat who will not drink still water', 'weight loss of 400 grams since spring', 'a refusal to eat the prescription food'],
    actions: ['weigh her on the same scale every fortnight', 'move fully to wet food before changing anything else', 'add a second water station away from the food', 'take the bloods before the dental, not after'],
    outcomes: ['a weight that stabilises', 'a cat drinking twice what she did', 'numbers that stop moving in the wrong direction', 'a diet she will actually eat'],
    asks: [
      'her creatinine went up again, how worried should I be',
      'she will not touch the renal food, what now',
      'how often should we be doing bloodwork',
      'is the weight loss enough to act on',
    ],
  },
  {
    key: 'photography',
    label: 'Film photography',
    subject: 'the darkroom',
    nouns: ['HP5 pushed to 1600', 'the enlarger head', 'the developer dilution', 'the contact sheet', 'the fixer'],
    issues: ['negatives that are thin in the shadows', 'dust that shows up on every print', 'a scan that clips the highlights', 'exhausted fixer leaving a stain'],
    actions: ['expose for the shadows and develop for the highlights', 'agitate for ten seconds every minute, not continuously', 'print a test strip before committing paper', 'replace the fixer by clip test, not by date'],
    outcomes: ['negatives with detail at both ends', 'prints without a speck on them', 'scans that hold the sky', 'chemistry you can trust'],
    asks: [
      'my shadows are empty, am I underexposing or underdeveloping',
      'how far can I push HP5 before it falls apart',
      'what dilution should I use for a softer contrast',
      'is it worth scanning at home or paying the lab',
    ],
  },
  {
    key: 'portuguese',
    label: 'Portuguese',
    subject: 'the lessons',
    nouns: ['the personal infinitive', 'the preterite', 'European versus Brazilian vowels', 'the subjunctive', 'shadowing practice'],
    issues: ['a listening gap that grammar drills do not close', 'vocabulary that will not stick past a week', 'mixing up ser and estar under pressure', 'reading fluently but freezing when spoken to'],
    actions: ['shadow ten minutes of native audio a day', 'switch flashcards from recognition to production', 'speak with a tutor twice a week rather than study four times', 'read one article aloud each morning'],
    outcomes: ['listening that keeps up with the news', 'words that survive a month', 'sentences that come out without assembly', 'a conversation that does not stall'],
    asks: [
      'why can I read this but not understand it spoken',
      'should I learn European or Brazilian first',
      'how do I stop mixing up ser and estar',
      'is shadowing actually better than flashcards',
    ],
  },
  {
    key: 'homelab',
    label: 'Home network',
    subject: 'the NAS',
    nouns: ['the ZFS pool', 'the 2.5 gig switch', 'the offsite backup', 'the reverse proxy', 'the UPS'],
    issues: ['a scrub that keeps finding checksum errors', 'a backup that has never been restored from', 'a single disk holding the only copy', 'a proxy that drops websockets'],
    actions: ['test a restore quarterly, not just the backup job', 'run mirrored vdevs rather than a wide raidz', 'put the offsite copy on a different medium', 'size the UPS for a clean shutdown, not for uptime'],
    outcomes: ['a pool that scrubs clean', 'a restore you have actually performed', 'three copies on two media with one offsite', 'shutdowns that never corrupt the pool'],
    asks: [
      'raidz2 or mirrors for eight drives',
      'how often should I be scrubbing the pool',
      'is my backup actually a backup if I have never restored it',
      'what size UPS do I need for this thing',
    ],
  },
  {
    key: 'woodwork',
    label: 'Woodworking',
    subject: 'the bench build',
    nouns: ['the laminated top', 'the leg vice', 'the dog holes', 'the mortise and tenon joinery', 'the finish schedule'],
    issues: ['a top that cups after a wet winter', 'a vice that racks under load', 'glue-up panic with too many clamps', 'tearout on figured stock'],
    actions: ['let the stock acclimatise for two weeks before milling', 'glue the top up in three sections rather than one', 'plane at a skew to beat the tearout', 'finish both faces so the top moves evenly'],
    outcomes: ['a top that stays flat through the seasons', 'a glue-up you can do without panicking', 'joinery that closes without a gap', 'a surface that takes finish evenly'],
    asks: [
      'how thick should the bench top actually be',
      'is a leg vice worth the extra work over a face vice',
      'how long should I let the boards sit before milling',
      'what finish will not gum up under hand planes',
    ],
  },
  {
    key: 'tea',
    label: 'Tea',
    subject: 'the tea shelf',
    nouns: ['a spring Longjing', 'the gaiwan', 'a shou puerh cake', 'water temperature', 'the storage tin'],
    issues: ['green tea going flat after two months', 'a brew that turns bitter on the second steep', 'storing everything in the same cupboard', 'water so hard it flattens the aroma'],
    actions: ['drop the water to 80C for the green', 'use short steeps and more leaf rather than the reverse', 'keep the puerh away from the greens', 'filter the water before it ever reaches the kettle'],
    outcomes: ['a green that stays sweet into the fourth steep', 'a cup with no astringency', 'a shelf where nothing takes on anything else', 'aroma that actually arrives'],
    asks: [
      'why does my green tea taste flat after a couple of months',
      'what temperature should I be using for this',
      'can I store puerh next to the greens',
      'how much leaf for a 120ml gaiwan',
    ],
  },
  {
    key: 'cycling',
    label: 'Cycling',
    subject: 'the bike',
    nouns: ['the rear derailleur', 'the tubeless setup', 'the bottom bracket', 'the chain wear indicator', 'the bar tape'],
    issues: ['a creak that only shows up under power', 'sealant that will not seat the bead', 'ghost shifting in the middle of the cassette', 'a chain worn past 0.75'],
    actions: ['check the cable tension before blaming the hanger', 'seat the tyre with a compressor before adding sealant', 'replace the chain at 0.5 rather than at 1.0', 'strip and regrease the seatpost first'],
    outcomes: ['a drivetrain that shifts silently', 'a tyre that holds pressure for a week', 'a creak traced to one interface', 'a cassette that lasts three chains'],
    asks: [
      'there is a creak under power, where do I start',
      'the tubeless will not seat, what am I doing wrong',
      'how worn is too worn for a chain',
      'is it the derailleur hanger or the cable',
    ],
  },
  {
    key: 'boardgame',
    label: 'Game design',
    subject: 'the prototype',
    nouns: ['the trading phase', 'the endgame trigger', 'the player count scaling', 'the rulebook draft', 'the blind playtest'],
    issues: ['a runaway leader by the third round', 'downtime that kills a four-player game', 'a rule everyone reads two different ways', 'an endgame that arrives before the engine does'],
    actions: ['playtest blind before you touch the balance again', 'cut the phase rather than adding a catch-up mechanic', 'write the rulebook as if you will never be in the room', 'trigger the endgame on a player action, not a round count'],
    outcomes: ['a lead that stays contestable', 'turns that overlap instead of queueing', 'a rulebook that survives without you', 'an ending players see coming one turn out'],
    asks: [
      'the leader runs away by round three, how do I fix it',
      'should I add a catch-up mechanic or cut a phase',
      'how many blind playtests before I trust the rules',
      'does this scale to five players or should I cap it at four',
    ],
  },
]

const FRAMES: string[] = [
  'The short answer is that {noun} is doing more work here than it looks, and {issue} is the symptom rather than the cause.',
  'If you {action}, the usual result is {outcome}, and you will know within a week or two whether it took.',
  'Worth separating two things: what {noun} is supposed to do, and what it is actually doing given {issue}.',
  'Most people reach for a bigger change at this point, but the cheap fix is to {action} and re-measure before anything else.',
  'I would not touch {noun} yet. Change one variable, write down what you saw, and only then decide.',
  'The failure mode you are describing — {issue} — almost always traces back to timing rather than to materials.',
  'Concretely: {action}. If that gets you {outcome}, you have found it; if not, we have ruled out the easy explanation.',
  'There is a version of this that works and a version that only appears to work, and the difference shows up under load.',
  'Keep a note of what {noun} looked like before and after. Without that you will be guessing again in a month.',
  'The trap is optimising for the thing that is easy to observe rather than the thing that is actually binding.',
  'Given {issue}, I would expect the next attempt to overshoot slightly. That is fine — overshoot once, then back off.',
  'You are close. {outcome} is a realistic target from where you are, and it does not need new equipment to get there.',
  'One caveat: this advice assumes conditions stay roughly where they are now. If they change, {noun} changes with them.',
  'It is worth writing down the rule you are actually following, because the one in your head and the one in practice have drifted.',
  'Do the boring diagnostic first. {action}, and only escalate if the result surprises you.',
  'The reason this keeps recurring is that {issue} is being treated as a one-off when it is really a standing condition.',
]

function pick<T>(items: T[], index: number): T {
  return items[((index % items.length) + items.length) % items.length]!
}

export interface MockEpisode {
  id: string
  turn_number: number
  topic: number
  user_message: string
  assistant_message: string
  created_at: string
}

/** Deterministic 32-bit PRNG. Same seed, same corpus, every time. */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function assistantBody(topic: Topic, salt: number, sentences: number): string {
  const parts: string[] = []
  for (let i = 0; i < sentences; i += 1) {
    const frame = pick(FRAMES, salt * 7 + i * 3)
    parts.push(
      frame
        .replace('{noun}', pick(topic.nouns, salt + i))
        .replace('{issue}', pick(topic.issues, salt * 2 + i))
        .replace('{action}', pick(topic.actions, salt * 3 + i))
        .replace('{outcome}', pick(topic.outcomes, salt + i * 2)),
    )
  }
  // Two paragraphs reads more like a real reply than one wall of text.
  const split = Math.max(2, Math.floor(parts.length / 2))
  return `${parts.slice(0, split).join(' ')}\n\n${parts.slice(split).join(' ')}`
}

/**
 * Build the store. Topics run in overlapping blocks so the recency window
 * covers a handful of subjects and the older store covers all sixteen —
 * which is the situation the coverage selector exists for.
 */
export function buildCorpus(count: number, seed = 5005): MockEpisode[] {
  const random = mulberry32(seed)
  const episodes: MockEpisode[] = []
  const start = Date.parse('2026-08-14T09:12:00Z')

  for (let i = 0; i < count; i += 1) {
    // Walk the topics in a shuffled-but-fixed order, revisiting each a few
    // times, so clusters have real internal structure.
    const topicIndex = (i * 7 + Math.floor(i / TOPICS.length) * 3) % TOPICS.length
    const topic = TOPICS[topicIndex]!
    const salt = i + topicIndex * 13

    // Sentence count drives episode size. A fifth of exchanges are short —
    // that is what lets a late candidate slip past a full budget under
    // skip-on-overflow packing.
    const roll = random()
    const sentences = roll < 0.2 ? 3 + Math.floor(random() * 3) : 24 + Math.floor(random() * 18)

    const ask = pick(topic.asks, salt)
    episodes.push({
      id: `ep-${String(i + 1).padStart(4, '0')}`,
      turn_number: i + 1,
      topic: topicIndex,
      user_message: `${ask}? Context: ${topic.subject} is where I keep getting stuck.`,
      assistant_message: assistantBody(topic, salt, sentences),
      created_at: new Date(start + i * 11 * 60_000).toISOString(),
    })
  }
  return episodes
}
