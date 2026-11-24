# Provisioning people with SCIM

Checkcord matches people across Slack, Teams, GitHub, Jira and the rest by email. SCIM 2.0 keeps its list of people in step with your identity provider: people are created, changed, deactivated and deleted there, and Checkcord follows.

The service is at `<PUBLIC_BASE_URL>/scim/v2`. Every request needs an admin key as the bearer token. Create one with `checkcord keys create --admin --name scim`.

These flows were built and tested against recorded Okta and Microsoft Entra ID request shapes. They have not been run against a live Okta or Entra tenant. Try one person and one group first.

## What is supported

- Discovery: `GET /ServiceProviderConfig`, `/ResourceTypes` and `/Schemas`.
- Users: create, read, list, replace (`PUT`), change (`PATCH`) and delete.
- Groups: create, read, list, replace, change members or name, and delete.
- Filters: `userName eq "..."` and `externalId eq "..."` on Users, `displayName eq "..."` and `externalId eq "..."` on Groups. Nothing else; other filters get a `400` with `scimType` `invalidFilter`.
- Paging with `startIndex` and `count`. At most 200 results a page.
- `excludedAttributes=members` on Groups.
- `PATCH` with `add`, `replace` and `remove`, with or without a `path`, in either case. Paths such as `active`, `name.givenName`, `emails[type eq "work"].value`, `members[value eq "ID"]` and enterprise extension attributes work. `"active": "False"` as a string is read as false.
- Attribute names are matched without regard to case.
- Errors use the SCIM error body, with `scimType` where one applies.

Not supported: bulk requests, sorting, ETags and password changes. A password sent by the provider is never stored.

## How a person is matched

- The email is the `emails` entry marked primary, else the `work` one, else the first, else `userName` when it is an address.
- `userName` and the email must each be unused by another person in the organization. A clash is a `409` with `scimType` `uniqueness`.
- A person created before SCIM, by sign-in or `checkcord keys create --create-user`, is found by `userName eq` on their email. The provider then links to them instead of creating a second person.
- Other attributes, including the enterprise extension, are kept as sent and returned. Checkcord does not act on them.

## Deactivating and deleting

- `active` false ends the person's sessions and stops their keys at once. Nothing is deleted.
- `DELETE /Users/{id}` deletes the person, their keys, the events they wrote and their place in every audience and group. See [self-hosting.md](self-hosting.md#deleting-a-person).

## Groups and admin rights

Groups are stored per organization. Members must be people Checkcord already has; unknown member ids are ignored and counted in the audit entry.

Set `SCIM_ADMIN_GROUPS` to the names of groups whose members are admins, comma-separated. Names are matched without regard to case. Then:

- A person becomes an admin when they join a listed group, and stops being one when they leave every listed group.
- Creating, renaming or deleting a listed group updates all its members.
- Each change is audited as `user.admin_changed` with source `scim_group`.
- Each sign-in sets the flag from the person's groups, so a person in no listed group is not an admin from their next sign-in on. A change made with `checkcord users admin` or the admin API lasts until that person's groups next change or they next sign in.
- Open sessions follow the flag at once.

**Precedence.** When `SCIM_ADMIN_GROUPS` is set, it alone decides the admin flag. The sign-in claim grants nothing, even with `OIDC_ADMIN_GROUPS_CLAIM` set. Admin API keys keep their own flag. Without `SCIM_ADMIN_GROUPS`, SCIM groups are stored but do not touch the flag, and the sign-in claim works as described in [sign-in.md](sign-in.md#admin-rights).

Groups also name audiences for connectors that have none of their own, such as Jira and Notion: `checkcord connections audience jira:HOST --group NAME`. A change to a group's members, a deleted group, or a person deactivated or deleted updates those audiences and the records under them. See [connectors.md](connectors.md#audiences-set-by-an-admin). Slack, Teams and GitHub audiences come from the source, not from groups.

## Audit

Every write is audited with the channel `scim`: `user.created`, `user.updated`, `user.deactivated`, `user.deleted`, `group.created`, `group.updated` (with counts of members added and removed, and the old name after a rename), `group.deleted` and `user.admin_changed`. Reads are not audited.

## Okta

1. In the Admin Console, open your Checkcord app integration (or create a **SWA** or **OIDC** app) and turn on **SCIM provisioning** on the **General** tab.
2. On the **Provisioning** tab, set:
   - **SCIM connector base URL**: `<PUBLIC_BASE_URL>/scim/v2`
   - **Unique identifier field for users**: `userName`
   - **Supported provisioning actions**: Push New Users, Push Profile Updates, Push Groups
   - **Authentication Mode**: HTTP Header, with the admin key as the bearer token
3. Click **Test Connector Configuration**, then save.
4. Under **To App**, enable **Create Users**, **Update User Attributes** and **Deactivate Users**. Leave **Sync Password** off.
5. Assign people or groups to the app. Okta looks each person up with `userName eq`, then creates or links them.
6. For admin rights, push a group such as `Checkcord Admins` on the **Push Groups** tab and set `SCIM_ADMIN_GROUPS=Checkcord Admins`.

Okta sends profile updates as `PUT` and deactivation as `PATCH` with `{"active": false}`. Group membership changes arrive as `PATCH` on the group.

## Microsoft Entra ID

1. In **Enterprise applications**, open your Checkcord app or create a non-gallery app. Open **Provisioning** and set the mode to **Automatic**.
2. **Tenant URL**: `<PUBLIC_BASE_URL>/scim/v2`. **Secret token**: the admin key. Click **Test Connection**.
3. Under **Mappings**, keep the default user mapping. `userPrincipalName` goes to `userName`, `mail` to `emails[type eq "work"].value` and the object id to `externalId`. If people's UPN differs from their email, check that `mail` is set for everyone.
4. Keep **Provision Microsoft Entra ID Groups** on if you want admin rights from a group.
5. Assign people and groups to the app, set **Provisioning Status** to **On** and save.
6. For admin rights, assign a group such as `Checkcord Admins` and set `SCIM_ADMIN_GROUPS=Checkcord Admins`.

Entra sends changes as `PATCH`, often with capitalized ops such as `Replace` and with `active` as the string `"False"`. Both are handled. Entra runs a cycle about every 40 minutes; use **Provision on demand** to test one person.

## Checking it

```bash
curl -s -H "Authorization: Bearer $ADMIN_KEY" "$PUBLIC_BASE_URL/scim/v2/ServiceProviderConfig"
curl -s -H "Authorization: Bearer $ADMIN_KEY" \
  "$PUBLIC_BASE_URL/scim/v2/Users?filter=userName%20eq%20%22someone%40example.com%22"
checkcord audit list --action user.created
```
