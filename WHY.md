# WHY.md
 
This app turns lab numbers into an explanation a person can read. It is never allowed to decide anything by itself, and most of the code exists to prove it stays inside that line.
 
---
 
## 1. What this is, and what it is not
 
### 1.1 Intended use
Person's blood panel goes in, then a plain-language profile comes out. This panel shows what each result means, and which values need attention. Then, a physician reviews every profile before the person sees it. Nothing reaches the customer without that review.
 
### 1.2 What it does not do
- Does not diagnose. It can say a value is low, however does not rationalize.
- Does not prescribe or recommend treatment.
- Does not decide urgency by itself. A certain rule-based protocol decides, then a physician confirms.
- It does not auto-release. A profile with no physician sign-off is a state the system refuses to enter. I wrote these as "does not" clauses on purpose. A boundary you can test beats a good intention you cannot.
 
### 1.3 Why the boundary is the hard part
"Explain without diagnosing" sounds like one rule. It is really a line I had to draw and then defend. "Your LDL is above the range printed on your report" is a description and is allowed. "You have high cholesterol" is a diagnosis and is not. Almost every decision below exists to keep the system on the safe side of that line, and to catch it the moment it drifts across.
 
---
 
## 2. The decision everything rests on
 
### 2.1 The model writes and does not decide
Every judgment about severity and urgency is made by a plain rules engine: lookups and comparisons with no AI. The language model only turns that decision into sentences.
 
Here is the comparison I could think of: Imagine asking one smart assistant to both spot the emergency and write the summary. Whether a life-threatening potassium gets recorded depends on how you happened to word your request to it. That is not something you can test and not a doctor should trust their patients to. 

So we split the job in two. The part that must never be wrong (like is this an emergency?) is code we can debug. The part where being wrong means a sentence (how do we say it kindly) is the only part that uses AI.
 
We rejected the single-model design for exactly that reason: it puts the life-or-death decision inside the component we least predict.
 
### 2.2 Made the invention of a "diagnosis" impossible
It is not enough to tell the model "do not make things up." So the rules engine's finding type has no field that can say "critical". Severity lives in a separate object only the rule engine creates. The narrative object can point at a finding the engine already made, but it has no way to create one. 

"The model invented a diagnosis" is therefore not a risk I hope to avoid. It is a shape the data types do not allow.
 
---
 
## 3. Data layer choices
 
### 3.1 Each result carries its own units and its own range
A "normal range" is not universal. It belongs to the exact lab, machine, and local population that produced it. The same value can be normal at one lab and high at another, and both will be right. So the range travels with each result. The engine reads it off the report and never looks one up.
 
I rejected the tidy option of one global range table. That table would have been the "invented reference range" failure in disguise, and worse, our own test suite would not have caught it, because the test would have graded against the same wrong table.
 
### 3.2 Never convert units and refuse mismatched ones
Converting everything to one unit might lead to a wrong factor turning that number into a misleading one. So we
store values as the lab reported them, and a validator refuses any result whose range is written in a different unit than the value. A milligram value compared against a millimole range is not a risk to watch for.
 
### 3.3 "Not measured" is not "measured as zero"
A test that was not run is not a result of zero. A glucose that was never ordered, read as zero, would look like somoene on the brink of death. So missingness is its own status with named reasons, kept apart from the number. Accessors return nothing when a value is absent like 'NULL'.
 
### 3.4 Untrusted text stays untrusted
Specimen comments carry real signal ("drawn above the IV line") and are also the one place an attacker could hide instructions. I do not delete them, because that throws away the signal. I wrap them so their default text form is a harmless marker. Getting the real text takes a deliberate call. As a direct result, the rules engine never reads comment text at all: any fact that must drive a rule (like blood clumping in the tube) is promoted to a structured coded field first. A rule that pattern-matched free text would be a hole in the one component whose whole job is to be predictable.
 
---
 
## 4. Rules engine choices
 
### 4.1 Urgency is the highest single finding, computed once, never edited after
The panel's urgency is the maximum across its findings, from emergency down to no action, computed one time at the end. No one can be edited afterward. This means a physician can read the finding list, apply the ordering by hand, and get the same answer the engine got. The engine cannot reach a conclusion it can't show its work for.
 
### 4.2 Adjustments only lower a finding, never raise it, and always leave a trace
When a result is unreliable (a falsely high potassium from a damaged sample), the engine lowers that finding to a capped level. Two rules I implemented:
- A damaged sample is evidence the number is unreliable. It is never evidence the patient is fine, because a
  real high potassium and a damaged sample can sit in the very same tube. Clearing on that note is how a real emergency gets missed.
- It leaves a trace. The finding keeps its original level on the record, and a companion "recollect" finding label is added at the same level. If someone later deletes the companion, the urgency does not silently collapse.
### 4.3 We did not invent an escalation level we were missing
The five urgency levels cannot say "the right action is draw the sample again." Rather than invent a sixth category, I route those cases to "urgent within 24 hours" and record the true intent in a separate field. We flag this as a real gap: the list needs a "recollect" value before launch.
 
### 4.4 I added two analytes because they were important
Potassium and ferritin were not on the original panel, but two required trap cases cannot exist without them. I labeled them as additions rather than folding them in quietly.
 
---
 
## 5. How I tested it
 
### 5.1 Two safety nets, in order: cheap deterministic gates first, then an LLM judge
The narrative output passes through two layers.
 
The gates are plain code. They check the text against a vocabulary: every number must trace to the panel, no made-up finding IDs, the next-step line must match the urgency tier, no prescription or invented-range wording. A gate has a zero percent miss rate on its own question, costs microseconds, and needs no network.
 
The judge is a model call that reads the narrative and the assessment together, so it can catch what the gates structurally cannot: a diagnosis or cause stated with only real numbers ("your low hemoglobin is because your iron is depleted"), or a treatment hint with no drug name ("consider iron-rich foods," the exact harm the
thalassemia case exists to catch).
 
Summary: the gates check the words against a list. Then, the judge checks the claim against the record. The judge runs second because anything a cheap certain check can settle should not be handed to an expensive probabilistic one.
 
### 5.2 The answer key is written before the numbers, so the test cannot grade itself
For our synthetic panels, the correct urgency label is authored first, then the numbers are generated to fit it. The generator imports no rules engine. If the test's answer key came from the same logic as the system, the test could only ever agree with itself and could never catch that logic being wrong.
 
### 5.3 I reported the disagreements
Over 600 synthetic panels the engine agrees with the pre-written answer key on 79.3 percent of cases. We changed no threshold to lift that number. The honest 79.3 with a full account of every disagreement are where the real findings are (section 7).
 
The number we care about most: all 38 true emergencies were caught, with zero false emergencies. Emergency recall is where a miss is unacceptable, and it is perfect on this corpus. However, I do not overclaim this because 38 is a small sample.
 
### 5.4 I also tested the test suite itself
A suite can pass because it is synthetic. So I deliberately break outputs (an invented range, a suppressed emergency, a planted diagnosis, an iron recommendation) and confirm each gate and the judge fires on its matching break. A green suite that has never been shown to catch a real fault is not evidence of anything.
 
### 5.5 The judge reports its own error rate, and cannot approve anything
The judge is graded against a small hand-labeled set, and the headline number is its false-negative rate on the safety categories, because a judge that misses diagnoses makes our "no diagnoses" claim fiction. These labels are self-assigned by the same author as the judge prompt, not clinician-verified, and every run says so.

A real clinician calibration is the first thing we would buy with more time. By design, a judge pass authorizes nothing: the field is named `no_objections`, not `approved`, and release still requires a physician. The judge can only ever add objections, and never remove the human.
 
---
 
## 6. What we did not build, and why
Each of these was a choice about where limited time paid off most:
 
1. Reading lab reports from PDF or scans. We take structured input. Parsing real report files is real, separate work.
2. Login and accounts. Not needed to show the system works.
3. Heavy statistics on our own sample size. The program just state the plain caution instead of dressing it up.
4. Full pregnancy-specific and child-specific range tables. This was handled by flagging it instead.
5. A formal written hazard analysis of the whole workflow. I name the main hazards in words.
6. Deep regulatory work. I noted where the product sits and move on.
7. An IV-line dilution rule. The coded field exists in the data, but a rule that adjusts a whole panel at once is a bigger decision than adjusting one value, so I left it for a person to design.
---
 
## 7. What we already know is wrong, and would fix next
I would deliberately hand this over with its faults named than let you guys find them voluntarily. In priority order:
 
1. Some analytes can never justify urgency, and the engine does not know that yet. A very low ferritin is far below its range, but nobody is hospitalized tonight for it. The engine over-escalates these. This one missing idea causes about a quarter of the disagreements. I left it out on purpose so the baseline numbers stay honest.
2. An invalid derived value that happens to land inside its printed range is flagged nowhere. An invalid Friedewald LDL of 83, sitting inside the normal range, produces no finding, so nothing marks it untrustworthy. The narrative is safe only by accident: the value never enters the payload, so the model cannot narrate it, but the physician view would still show "LDL 83, normal" with no caveat. 16 of 61 eval cases carry a value like this. The data layer already knows (`BloodPanel.untrustworthy_values()`), and the UI now surfaces it, but the real fix belongs in the engine: emit an informational finding for any present-but-untrustworthy value even when it does not flag.
3. No pregnancy-aware ranges. The engine compares a pregnant patient against the ordinary adult column the lab printed and calls normal pregnancy changes a problem. This is a designed trap and the engine fails it completely today.
4. No pattern rules. Some findings live in a combination of values, not any single one (a regional anemia pattern is the example). A one-value-at-a-time engine cannot see them.
5. Kidney scoring is too soft. A meaningfully reduced kidney number reads as mild because it is only a little outside range. It needs standard staging bands, like the ones hemoglobin and platelets already have.
6. One instruction contradicts one answer-key label: a not-ordered test was told to produce no finding, but the key expects a routine "please complete this test" note. I just followed the instruction literally and flagged the conflict rather than picking a side. This was my independent decision.
---
 
## 8. The caution before any of this touches a real person
The reference values and critical thresholds here are plausible placeholders, not approved clinical policy. 

Before launch, these numbers must be replaced with values transcribed from the actual partner healthcare and signed off by a named medical director.