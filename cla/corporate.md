# MartianSecurity Software Grant and Corporate Contributor License Agreement

This Agreement is based on The Apache Software Foundation's
[Software Grant and Corporate Contributor License Agreement (v r190612)](https://www.apache.org/licenses/cla-corporate.pdf).
Its terms are the Foundation's, with these changes: MartianSecurity takes the place of the
Foundation; the Foundation's undertaking about its nonprofit status and bylaws is left out,
because it applies only to the Foundation; and the paper form is replaced by a pull request, as
[How to sign](#how-to-sign) describes.

A corporation signs this Agreement in addition to, not instead of, the
[Individual Contributor License Agreement](individual.md): each of its designated employees still
signs the individual agreement on their own pull requests, and this Agreement covers the
corporation's rights in their work (section 4 of the individual agreement).

## Software Grant and Corporate Contributor License Agreement ("Agreement")

Thank you for your interest in MartianSecurity. In order to clarify the intellectual property
license granted with Contributions from any person or entity, MartianSecurity must have a
Contributor License Agreement (CLA) on file that has been signed by each Contributor, indicating
agreement to the license terms below. This license is for your protection as a Contributor as
well as the protection of MartianSecurity and its users; it does not change your rights to use
your own Contributions for any other purpose.

This version of the Agreement allows an entity (the "Corporation") to submit Contributions to
MartianSecurity, to authorize Contributions submitted by its designated employees to
MartianSecurity, and to grant copyright and patent licenses thereto.

You accept and agree to the following terms and conditions for Your present and future
Contributions submitted to MartianSecurity. Except for the license granted herein to
MartianSecurity and recipients of software distributed by MartianSecurity, You reserve all right,
title, and interest in and to Your Contributions.

1. Definitions.

   "You" (or "Your") shall mean the copyright owner or legal entity authorized by the copyright
   owner that is making this Agreement with MartianSecurity. For legal entities, the entity making
   a Contribution and all other entities that control, are controlled by, or are under common
   control with that entity are considered to be a single Contributor. For the purposes of this
   definition, "control" means (i) the power, direct or indirect, to cause the direction or
   management of such entity, whether by contract or otherwise, or (ii) ownership of fifty
   percent (50%) or more of the outstanding shares, or (iii) beneficial ownership of such entity.

   "Contribution" shall mean the code, documentation or other original works of authorship
   expressly identified in Schedule B, as well as any original work of authorship, including any
   modifications or additions to an existing work, that is intentionally submitted by You to
   MartianSecurity for inclusion in, or documentation of, any of the products owned or managed by
   MartianSecurity (the "Work"). For the purposes of this definition, "submitted" means any form
   of electronic, verbal, or written communication sent to MartianSecurity or its
   representatives, including but not limited to communication on electronic mailing lists,
   source code control systems, and issue tracking systems that are managed by, or on behalf of,
   MartianSecurity for the purpose of discussing and improving the Work, but excluding
   communication that is conspicuously marked or otherwise designated in writing by You as "Not a
   Contribution."

2. Grant of Copyright License. Subject to the terms and conditions of this Agreement, You hereby
   grant to MartianSecurity and to recipients of software distributed by MartianSecurity a
   perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable copyright license to
   reproduce, prepare derivative works of, publicly display, publicly perform, sublicense, and
   distribute Your Contributions and such derivative works.

3. Grant of Patent License. Subject to the terms and conditions of this Agreement, You hereby
   grant to MartianSecurity and to recipients of software distributed by MartianSecurity a
   perpetual, worldwide, non-exclusive, no-charge, royalty-free, irrevocable (except as stated in
   this section) patent license to make, have made, use, offer to sell, sell, import, and
   otherwise transfer the Work, where such license applies only to those patent claims licensable
   by You that are necessarily infringed by Your Contribution(s) alone or by combination of Your
   Contribution(s) with the Work to which such Contribution(s) were submitted. If any entity
   institutes patent litigation against You or any other entity (including a cross-claim or
   counterclaim in a lawsuit) alleging that your Contribution, or the Work to which you have
   contributed, constitutes direct or contributory patent infringement, then any patent licenses
   granted to that entity under this Agreement for that Contribution or Work shall terminate as of
   the date such litigation is filed.

4. You represent that You are legally entitled to grant the above license. You represent further
   that each employee of the Corporation designated on Schedule A below (or in a subsequent
   written modification to that Schedule) is authorized to submit Contributions on behalf of the
   Corporation.

5. You represent that each of Your Contributions is Your original creation (see section 7 for
   submissions on behalf of others).

6. You are not expected to provide support for Your Contributions, except to the extent You
   desire to provide support. You may provide support for free, for a fee, or not at all. Unless
   required by applicable law or agreed to in writing, You provide Your Contributions on an "AS
   IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied, including,
   without limitation, any warranties or conditions of TITLE, NON-INFRINGEMENT, MERCHANTABILITY, or
   FITNESS FOR A PARTICULAR PURPOSE.

7. Should You wish to submit work that is not Your original creation, You may submit it to
   MartianSecurity separately from any Contribution, identifying the complete details of its
   source and of any license or other restriction (including, but not limited to, related
   patents, trademarks, and license agreements) of which you are personally aware, and
   conspicuously marking the work as "Submitted on behalf of a third-party: [named here]".

8. It is your responsibility to notify MartianSecurity when any change is required to the list of
   designated employees authorized to submit Contributions on behalf of the Corporation, or to the
   Corporation's Point of Contact with MartianSecurity.

Schedule A: initial list of designated employees. NB: authorization is not tied to particular
Contributions.

Schedule B: identification of optional concurrent software grant. Would be left blank or omitted
if there is no concurrent software grant.

## How to sign

A person authorized to sign for the Corporation opens a pull request to
[MorningHQ/martian-redteam](https://github.com/MorningHQ/martian-redteam), from their own GitHub
account, that adds the Corporation to [corporations.md](corporations.md) with this entry filled
in:

```markdown
## Corporation name

- Corporation address: registered address
- Point of Contact: name, GitHub account `account`
- Signed by: name, title, GitHub account `account`
- Date: YYYY-MM-DD
- Schedule A, designated employees: GitHub accounts `account`, `account`
- Schedule B, concurrent software grant: none, or the code, documentation or other works granted
```

Opening the pull request with the entry is the signature, and the Agreement is on file with
MartianSecurity once a maintainer merges it. The entry is public, so it holds no e-mail address
or telephone number: MartianSecurity reaches the Point of Contact through their GitHub account.

A later pull request that changes the entry, from the person who signed or the Point of Contact,
is a written modification of Schedule A, or the notice that section 8 asks for. The pull request
is checked like any other, so its author signs the individual agreement in it as well.
