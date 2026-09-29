"""GraphQL documents used by the GitHub backend."""

REACTIONS = "reactionGroups { content viewerHasReacted reactors { totalCount } }"

REVIEW_COMMENT_FIELDS = f"""
fragment ReviewCommentFields on PullRequestReviewComment {{
  id body createdAt updatedAt lastEditedAt url state
  author {{ login }}
  authorAssociation
  viewerCanUpdate viewerCanDelete viewerCanMinimize viewerCanUnminimize viewerCanReact viewerDidAuthor
  isMinimized minimizedReason
  diffHunk
  pullRequestReview {{ id }}
  replyTo {{ id }}
  {REACTIONS}
}}
"""

THREAD_FIELDS = """
fragment ThreadFields on PullRequestReviewThread {
  id path line startLine originalLine originalStartLine diffSide startDiffSide
  isResolved isOutdated subjectType
  viewerCanResolve viewerCanUnresolve viewerCanReply
  resolvedBy { login }
  comments(first: 100) { nodes { ...ReviewCommentFields } }
}
"""

ISSUE_COMMENT_FIELDS = f"""
fragment IssueCommentFields on IssueComment {{
  id body createdAt updatedAt lastEditedAt url
  author {{ login }}
  authorAssociation
  viewerCanUpdate viewerCanDelete viewerCanMinimize viewerCanUnminimize viewerCanReact viewerDidAuthor
  isMinimized minimizedReason
  {REACTIONS}
}}
"""

REVIEW_FIELDS = f"""
fragment ReviewFields on PullRequestReview {{
  id state body createdAt submittedAt url
  author {{ login }}
  viewerCanUpdate viewerCanDelete viewerCanMinimize viewerCanUnminimize viewerDidAuthor
  isMinimized minimizedReason
  comments {{ totalCount }}
  {REACTIONS}
}}
"""

PAGE = "pageInfo { hasNextPage endCursor }"

PULL_REQUEST = (
    f"""
query PullRequest($owner: String!, $name: String!, $number: Int!) {{
  viewer {{ login }}
  repository(owner: $owner, name: $name) {{
    pullRequest(number: $number) {{
      id number title body url state isDraft createdAt updatedAt
      author {{ login }}
      baseRefName headRefName baseRefOid headRefOid
      headRepository {{ nameWithOwner }}
      additions deletions changedFiles
      mergeable reviewDecision viewerDidAuthor
      labels(first: 20) {{ nodes {{ name color }} }}
      reviewRequests(first: 30) {{
        nodes {{ requestedReviewer {{
          __typename
          ... on User {{ login }}
          ... on Team {{ slug }}
          ... on Bot {{ login }}
          ... on Mannequin {{ login }}
        }} }}
      }}
      latestReviews(first: 50) {{ nodes {{ author {{ login }} state }} }}
      headCommit: commits(last: 1) {{ totalCount nodes {{ commit {{ oid statusCheckRollup {{ state }} }} }} }}
      commits(first: 100) {{ nodes {{ commit {{ oid messageHeadline authoredDate author {{ name user {{ login }} }} }} }} }}
      files(first: 100) {{ {PAGE} nodes {{ path additions deletions changeType viewerViewedState }} }}
      reviewThreads(first: 100) {{ {PAGE} nodes {{ ...ThreadFields }} }}
      comments(first: 100) {{ {PAGE} nodes {{ ...IssueCommentFields }} }}
      reviews(first: 100) {{ {PAGE} nodes {{ ...ReviewFields }} }}
    }}
  }}
}}
"""
    + THREAD_FIELDS
    + REVIEW_COMMENT_FIELDS
    + ISSUE_COMMENT_FIELDS
    + REVIEW_FIELDS
)

# Follow-up pages for the connections above, fetched through the PR's node id.
MORE_FILES = f"""
query MoreFiles($id: ID!, $after: String!) {{
  node(id: $id) {{ ... on PullRequest {{
    files(first: 100, after: $after) {{ {PAGE} nodes {{ path additions deletions changeType viewerViewedState }} }}
  }} }}
}}
"""

MORE_THREADS = (
    f"""
query MoreThreads($id: ID!, $after: String!) {{
  node(id: $id) {{ ... on PullRequest {{
    reviewThreads(first: 100, after: $after) {{ {PAGE} nodes {{ ...ThreadFields }} }}
  }} }}
}}
"""
    + THREAD_FIELDS
    + REVIEW_COMMENT_FIELDS
)

MORE_COMMENTS = (
    f"""
query MoreComments($id: ID!, $after: String!) {{
  node(id: $id) {{ ... on PullRequest {{
    comments(first: 100, after: $after) {{ {PAGE} nodes {{ ...IssueCommentFields }} }}
  }} }}
}}
"""
    + ISSUE_COMMENT_FIELDS
)

MORE_REVIEWS = (
    f"""
query MoreReviews($id: ID!, $after: String!) {{
  node(id: $id) {{ ... on PullRequest {{
    reviews(first: 100, after: $after) {{ {PAGE} nodes {{ ...ReviewFields }} }}
  }} }}
}}
"""
    + REVIEW_FIELDS
)

ADD_REVIEW = (
    """
mutation AddReview($input: AddPullRequestReviewInput!) {
  addPullRequestReview(input: $input) { pullRequestReview { ...ReviewFields } }
}
"""
    + REVIEW_FIELDS
)

SUBMIT_REVIEW = (
    """
mutation SubmitReview($input: SubmitPullRequestReviewInput!) {
  submitPullRequestReview(input: $input) { pullRequestReview { ...ReviewFields } }
}
"""
    + REVIEW_FIELDS
)

DELETE_REVIEW = """
mutation DeleteReview($input: DeletePullRequestReviewInput!) {
  deletePullRequestReview(input: $input) { pullRequestReview { id } }
}
"""

ADD_THREAD = (
    """
mutation AddThread($input: AddPullRequestReviewThreadInput!) {
  addPullRequestReviewThread(input: $input) { thread { ...ThreadFields } }
}
"""
    + THREAD_FIELDS
    + REVIEW_COMMENT_FIELDS
)

ADD_REPLY = (
    """
mutation AddReply($input: AddPullRequestReviewThreadReplyInput!) {
  addPullRequestReviewThreadReply(input: $input) { comment { ...ReviewCommentFields } }
}
"""
    + REVIEW_COMMENT_FIELDS
)

UPDATE_REVIEW_COMMENT = (
    """
mutation UpdateReviewComment($input: UpdatePullRequestReviewCommentInput!) {
  updatePullRequestReviewComment(input: $input) { pullRequestReviewComment { ...ReviewCommentFields } }
}
"""
    + REVIEW_COMMENT_FIELDS
)

DELETE_REVIEW_COMMENT = """
mutation DeleteReviewComment($input: DeletePullRequestReviewCommentInput!) {
  deletePullRequestReviewComment(input: $input) { clientMutationId }
}
"""

UPDATE_REVIEW = (
    """
mutation UpdateReview($input: UpdatePullRequestReviewInput!) {
  updatePullRequestReview(input: $input) { pullRequestReview { ...ReviewFields } }
}
"""
    + REVIEW_FIELDS
)

RESOLVE_THREAD = """
mutation Resolve($input: ResolveReviewThreadInput!) {
  resolveReviewThread(input: $input) {
    thread { id isResolved viewerCanResolve viewerCanUnresolve resolvedBy { login } }
  }
}
"""

UNRESOLVE_THREAD = """
mutation Unresolve($input: UnresolveReviewThreadInput!) {
  unresolveReviewThread(input: $input) {
    thread { id isResolved viewerCanResolve viewerCanUnresolve resolvedBy { login } }
  }
}
"""

ADD_COMMENT = (
    """
mutation AddComment($input: AddCommentInput!) {
  addComment(input: $input) { commentEdge { node { ...IssueCommentFields } } }
}
"""
    + ISSUE_COMMENT_FIELDS
)

UPDATE_ISSUE_COMMENT = (
    """
mutation UpdateIssueComment($input: UpdateIssueCommentInput!) {
  updateIssueComment(input: $input) { issueComment { ...IssueCommentFields } }
}
"""
    + ISSUE_COMMENT_FIELDS
)

DELETE_ISSUE_COMMENT = """
mutation DeleteIssueComment($input: DeleteIssueCommentInput!) {
  deleteIssueComment(input: $input) { clientMutationId }
}
"""

MINIMIZE = """
mutation Minimize($input: MinimizeCommentInput!) {
  minimizeComment(input: $input) { minimizedComment { isMinimized minimizedReason } }
}
"""

UNMINIMIZE = """
mutation Unminimize($input: UnminimizeCommentInput!) {
  unminimizeComment(input: $input) { unminimizedComment { isMinimized minimizedReason } }
}
"""

MARK_VIEWED = """
mutation MarkViewed($input: MarkFileAsViewedInput!) {
  markFileAsViewed(input: $input) { clientMutationId }
}
"""

UNMARK_VIEWED = """
mutation UnmarkViewed($input: UnmarkFileAsViewedInput!) {
  unmarkFileAsViewed(input: $input) { clientMutationId }
}
"""

ADD_REACTION = """
mutation AddReaction($input: AddReactionInput!) {
  addReaction(input: $input) { reaction { content } }
}
"""

REMOVE_REACTION = """
mutation RemoveReaction($input: RemoveReactionInput!) {
  removeReaction(input: $input) { reaction { content } }
}
"""

# Deliberately light: per-PR fields like CI status, requested reviewers and diff size are
# expensive for GitHub to compute, so they're fetched afterwards with PR_DETAILS.
SEARCH_PULL_REQUESTS = """
query SearchPRs($query: String!) {
  viewer { login }
  search(query: $query, type: ISSUE, first: 50) {
    issueCount
    nodes {
      ... on PullRequest {
        id number title isDraft createdAt updatedAt headRefName
        author { login }
        repository { name owner { login } }
        labels(first: 6) { nodes { name color } }
        latestReviews(first: 30) { nodes { author { login } state } }
      }
    }
  }
}
"""

PR_DETAILS = """
query Details($ids: [ID!]!) {
  viewer { login }
  nodes(ids: $ids) {
    ... on PullRequest {
      id reviewDecision additions deletions
      comments { totalCount }
      assignees(first: 10) { nodes { login } }
      reviewRequests(first: 20) {
        nodes { requestedReviewer { __typename ... on User { login } ... on Team { combinedSlug } } }
      }
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    }
  }
}
"""


def viewed_mutation(count: int, viewed: bool) -> str:
    """Mark `count` files ($p0..$pN) as viewed or not, in one request."""
    name = "markFileAsViewed" if viewed else "unmarkFileAsViewed"
    params = ", ".join(f"$p{i}: String!" for i in range(count))
    fields = "\n".join(
        f"  m{i}: {name}(input: {{pullRequestId: $pr, path: $p{i}}}) {{ clientMutationId }}"
        for i in range(count)
    )
    return f"mutation Viewed($pr: ID!, {params}) {{\n{fields}\n}}"


def blobs_query(count: int) -> str:
    """A query fetching `count` blobs by "<oid>:<path>" expressions ($e0..$eN)."""
    params = ", ".join(f"$e{i}: String!" for i in range(count))
    fields = "\n".join(
        f"    f{i}: object(expression: $e{i}) {{ ... on Blob {{ text isBinary isTruncated byteSize }} }}"
        for i in range(count)
    )
    return f"""
query Blobs($owner: String!, $name: String!, {params}) {{
  repository(owner: $owner, name: $name) {{
{fields}
  }}
}}
"""
