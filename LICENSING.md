# Licensing

Recollect is **dual licensed**. You choose which licence you take it under.

| | **AGPL-3.0-or-later** | **Commercial licence** |
|---|---|---|
| Cost | Free | Paid |
| Try it, read it, modify it privately | Yes | Yes |
| Run it for yourself, on your own machine | Yes | Yes |
| Deploy it, or offer it to others over a network | Yes, **if** you release your complete source under the AGPL | Yes, with no source-release obligation |
| Ship it inside a closed-source product | No | Yes |
| Keep your modifications private | No | Yes |

Copyright © 2026 Idris Applied AI Research.

---

## The default: AGPL-3.0-or-later

Unless you hold a separate written agreement with Idris Applied AI Research,
you receive Recollect under the **GNU Affero General Public License, version 3
or (at your option) any later version**. The full text is in
[`LICENSE`](LICENSE).

The AGPL is a real open-source licence and it grants real rights. You may run
the software, study it, change it, and redistribute it. You may deploy it,
including commercially.

What it asks in return is reciprocity, and one clause makes that unusually
strong for server software:

> **Section 13 — Remote Network Interaction.** If you modify Recollect and let
> users interact with it remotely over a network, you must offer those users
> the complete corresponding source of your modified version, under the AGPL.

Ordinary copyleft is triggered by *distribution*. Section 13 is triggered by
*use over a network*. Running a modified Recollect as an internal tool your
staff reach over the network, or as a product your customers reach over the
internet, both engage it. There is no "we never shipped a copy, so we never
distributed it" exemption, which is precisely the gap the Affero clause was
written to close.

Two consequences worth stating plainly, because they are the ones people get
wrong:

- **Evaluating Recollect costs nothing and requires no permission.** Clone it,
  run it, benchmark it, take it apart. Private use, including inside a company,
  triggers nothing on its own.
- **Deploying it is allowed too** — but under the AGPL, the whole of your
  deployed work has to be available to its users under the AGPL. For most
  commercial deployments that is the part that does not work, and that is what
  the commercial licence is for.

If you are not sure whether your intended use engages section 13, ask before
you build on it, not after.

## The alternative: a commercial licence

A commercial licence removes the AGPL's reciprocity obligations. It is the
right choice if you want to:

- deploy Recollect, or something built on it, as a hosted or customer-facing
  service without publishing your source;
- embed it in a proprietary product you distribute;
- keep your own modifications, prompts, integrations, or surrounding system
  private;
- take it under terms that include warranty, indemnity, or support, none of
  which the AGPL provides.

Commercial licences, evaluation licences, and deployment rights are available.
Write to **idrisappliedairesearch@gmail.com** with a description of what you
want to build and how it would be deployed.

## Contributions

If you submit a contribution, you assign to Idris Applied AI Research all
right, title, and interest in it, or — where assignment is not possible — grant
a perpetual, worldwide, irrevocable, royalty-free licence to use it for any
purpose without restriction or attribution.

This is not boilerplate, and it is not a land grab. Dual licensing only works
if one party holds the rights to the whole codebase: a commercial licence
cannot be granted over a contribution that its author licensed to the project
under the AGPL alone. Every dual-licensed project needs this, and saying so is
better than burying it.

If you cannot make that grant, say so before your contribution is reviewed.

## `episodic`

Recollect builds on the `episodic` library, which is published separately from
the [contextDecayWindow](https://github.com/IdrisAppliedAIResearch/contextDecayWindow)
repository under the same dual model: AGPL-3.0-or-later, or a commercial
licence from Idris Applied AI Research.

Because both are dual licensed on the same terms by the same copyright holder,
the two move together. Take Recollect under the AGPL and `episodic` comes to
you under the AGPL. A commercial licence for a deployment covers both; you do
not need to negotiate them separately.

## No warranty

Recollect is provided without warranty of any kind, to the extent permitted by
applicable law. See sections 15 to 17 of [`LICENSE`](LICENSE) for the full
disclaimer that applies to the AGPL grant. Different terms may be negotiated as
part of a commercial licence.

## Other components

Recollect depends on third-party components with their own licences, and on
model weights that are not distributed with it at all.
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) records every one of them.
Nothing in this document grants, restricts, or modifies any right in those
components.

---

*Questions about any of this, including whether you need a commercial licence:*
**idrisappliedairesearch@gmail.com**
