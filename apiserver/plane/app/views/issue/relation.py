# Python imports
import json

# Django imports
from django.utils import timezone
from django.db.models import (
    Q,
    OuterRef,
    F,
    Func,
    UUIDField,
    Value,
    CharField,
    Case,
    When,
)
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models.functions import Coalesce
from django.contrib.postgres.aggregates import ArrayAgg
from django.contrib.postgres.fields import ArrayField

# Third Party imports
from rest_framework.response import Response
from rest_framework import status

# Module imports
from .. import BaseViewSet
from plane.app.serializers import (
    IssueRelationSerializer,
    RelatedIssueSerializer,
)
from plane.app.permissions import ProjectEntityPermission
from plane.db.models import (
    Project,
    IssueRelation,
    Issue,
    FileAsset,
    IssueLink,
)
from plane.bgtasks.issue_activities_task import issue_activity


class IssueRelationViewSet(BaseViewSet):
    serializer_class = IssueRelationSerializer
    model = IssueRelation
    permission_classes = [
        ProjectEntityPermission,
    ]

    def list(self, request, slug, project_id, issue_id):
        issue_relations = (
            IssueRelation.objects.filter(
                Q(issue_id=issue_id) | Q(related_issue=issue_id)
            )
            .filter(workspace__slug=self.kwargs.get("slug"))
            .select_related("project")
            .select_related("workspace")
            .select_related("issue")
            .order_by("-created_at")
            .distinct()
        )
        # get all blocking issues
        blocking_issues = issue_relations.filter(
            relation_type="blocked_by", related_issue_id=issue_id
        ).values_list("issue_id", flat=True)

        # get all blocked by issues
        blocked_by_issues = issue_relations.filter(
            relation_type="blocked_by", issue_id=issue_id
        ).values_list("related_issue_id", flat=True)

        # get all duplicate issues
        duplicate_issues = issue_relations.filter(
            issue_id=issue_id, relation_type="duplicate"
        ).values_list("related_issue_id", flat=True)

        # get all relates to issues
        duplicate_issues_related = issue_relations.filter(
            related_issue_id=issue_id, relation_type="duplicate"
        ).values_list("issue_id", flat=True)

        # get all relates to issues
        relates_to_issues = issue_relations.filter(
            issue_id=issue_id, relation_type="relates_to"
        ).values_list("related_issue_id", flat=True)

        # get all relates to issues
        relates_to_issues_related = issue_relations.filter(
            related_issue_id=issue_id, relation_type="relates_to"
        ).values_list("issue_id", flat=True)

        queryset = (
            Issue.issue_objects.filter(workspace__slug=slug)
            .select_related("workspace", "project", "state", "parent")
            .prefetch_related("assignees", "labels", "issue_module__module")
            .annotate(
                cycle_id=Case(
                    When(
                        issue_cycle__cycle__deleted_at__isnull=True,
                        then=F("issue_cycle__cycle_id"),
                    ),
                    default=None,
                )
            )
            .annotate(
                link_count=IssueLink.objects.filter(issue=OuterRef("id"))
                .order_by()
                .annotate(count=Func(F("id"), function="Count"))
                .values("count")
            )
            .annotate(
                attachment_count=FileAsset.objects.filter(
                    issue_id=OuterRef("id"),
                    entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
                )
                .order_by()
                .annotate(count=Func(F("id"), function="Count"))
                .values("count")
            )
            .annotate(
                sub_issues_count=Issue.issue_objects.filter(
                    parent=OuterRef("id")
                )
                .order_by()
                .annotate(count=Func(F("id"), function="Count"))
                .values("count")
            )
            .annotate(
                label_ids=Coalesce(
                    ArrayAgg(
                        "labels__id",
                        distinct=True,
                        filter=(
                            ~Q(labels__id__isnull=True)
                            & Q(labels__deleted_at__isnull=True)
                        ),
                    ),
                    Value([], output_field=ArrayField(UUIDField())),
                ),
                assignee_ids=Coalesce(
                    ArrayAgg(
                        "assignees__id",
                        distinct=True,
                        filter=~Q(assignees__id__isnull=True)
                        & Q(assignees__member_project__is_active=True),
                    ),
                    Value([], output_field=ArrayField(UUIDField())),
                ),
            )
        ).distinct()

        # Fields
        fields = [
            "id",
            "name",
            "state_id",
            "sort_order",
            "priority",
            "sequence_id",
            "project_id",
            "label_ids",
            "assignee_ids",
            "created_at",
            "updated_at",
            "created_by",
            "updated_by",
            "relation_type",
        ]

        response_data = {
            "blocking": queryset.filter(pk__in=blocking_issues)
            .annotate(
                relation_type=Value("blocking", output_field=CharField())
            )
            .values(*fields),
            "blocked_by": queryset.filter(pk__in=blocked_by_issues)
            .annotate(
                relation_type=Value("blocked_by", output_field=CharField())
            )
            .values(*fields),
            "duplicate": queryset.filter(pk__in=duplicate_issues)
            .annotate(
                relation_type=Value(
                    "duplicate",
                    output_field=CharField(),
                )
            )
            .values(*fields)
            | queryset.filter(pk__in=duplicate_issues_related)
            .annotate(
                relation_type=Value(
                    "duplicate",
                    output_field=CharField(),
                )
            )
            .values(*fields),
            "relates_to": queryset.filter(pk__in=relates_to_issues)
            .annotate(
                relation_type=Value(
                    "relates_to",
                    output_field=CharField(),
                )
            )
            .values(*fields)
            | queryset.filter(pk__in=relates_to_issues_related)
            .annotate(
                relation_type=Value(
                    "relates_to",
                    output_field=CharField(),
                )
            )
            .values(*fields),
        }

        return Response(response_data, status=status.HTTP_200_OK)

    def create(self, request, slug, project_id, issue_id):
        relation_type = request.data.get("relation_type", None)
        issues = request.data.get("issues", [])
        project = Project.objects.get(pk=project_id)

        issue_relation = IssueRelation.objects.bulk_create(
            [
                IssueRelation(
                    issue_id=(
                        issue if relation_type == "blocking" else issue_id
                    ),
                    related_issue_id=(
                        issue_id if relation_type == "blocking" else issue
                    ),
                    relation_type=(
                        "blocked_by"
                        if relation_type == "blocking"
                        else relation_type
                    ),
                    project_id=project_id,
                    workspace_id=project.workspace_id,
                    created_by=request.user,
                    updated_by=request.user,
                )
                for issue in issues
            ],
            batch_size=10,
            ignore_conflicts=True,
        )

        issue_activity.delay(
            type="issue_relation.activity.created",
            requested_data=json.dumps(request.data, cls=DjangoJSONEncoder),
            actor_id=str(request.user.id),
            issue_id=str(issue_id),
            project_id=str(project_id),
            current_instance=None,
            epoch=int(timezone.now().timestamp()),
            notification=True,
            origin=request.META.get("HTTP_ORIGIN"),
        )

        if relation_type == "blocking":
            return Response(
                RelatedIssueSerializer(issue_relation, many=True).data,
                status=status.HTTP_201_CREATED,
            )
        else:
            return Response(
                IssueRelationSerializer(issue_relation, many=True).data,
                status=status.HTTP_201_CREATED,
            )

    def remove_relation(self, request, slug, project_id, issue_id):
        relation_type = request.data.get("relation_type", None)
        related_issue = request.data.get("related_issue", None)

        if relation_type == "blocking":
            issue_relation = IssueRelation.objects.get(
                workspace__slug=slug,
                project_id=project_id,
                issue_id=related_issue,
                related_issue_id=issue_id,
            )
        else:
            issue_relation = IssueRelation.objects.get(
                workspace__slug=slug,
                project_id=project_id,
                issue_id=issue_id,
                related_issue_id=related_issue,
            )
        current_instance = json.dumps(
            IssueRelationSerializer(issue_relation).data,
            cls=DjangoJSONEncoder,
        )
        issue_relation.delete(soft=False)
        issue_activity.delay(
            type="issue_relation.activity.deleted",
            requested_data=json.dumps(request.data, cls=DjangoJSONEncoder),
            actor_id=str(request.user.id),
            issue_id=str(issue_id),
            project_id=str(project_id),
            current_instance=current_instance,
            epoch=int(timezone.now().timestamp()),
            notification=True,
            origin=request.META.get("HTTP_ORIGIN"),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
