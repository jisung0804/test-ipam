from rest_framework import status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from netbox.api.viewsets import NetBoxModelViewSet

from .. import logic
from ..models import Discrepancy, IPRequest
from .serializers import DiscrepancySerializer, IPRequestSerializer


class IPRequestViewSet(NetBoxModelViewSet):
    queryset = IPRequest.objects.all()
    serializer_class = IPRequestSerializer

    def _need_change_perm(self, request):
        return request.user.has_perm('netbox_ip_request.change_iprequest')

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        if not self._need_change_perm(request):
            return Response({'detail': '승인 권한 없음'}, status=status.HTTP_403_FORBIDDEN)
        try:
            ip = logic.approve(int(pk), request.user.username)
        except logic.AllocationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_409_CONFLICT)
        return Response({'ip_address': str(ip), 'ip_id': ip.pk})

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        if not self._need_change_perm(request):
            return Response({'detail': '반려 권한 없음'}, status=status.HTTP_403_FORBIDDEN)
        try:
            logic.reject(int(pk), request.user.username, request.data.get('reason', ''))
        except logic.AllocationError as e:
            return Response({'detail': str(e)}, status=status.HTTP_409_CONFLICT)
        return Response({'status': 'rejected'})


class DiscrepancyViewSet(NetBoxModelViewSet):
    queryset = Discrepancy.objects.all()
    serializer_class = DiscrepancySerializer


class _CollectorView(APIView):
    permission_classes = [IsAuthenticated]

    def check_perm(self, request):
        return request.user.has_perm('netbox_ip_request.add_discrepancy')


class ArpIngestView(_CollectorView):
    """POST {"device": "core-1", "entries": [["10.0.0.5", "00:50:56:..", "irb.101"], ...]}"""
    def post(self, request):
        if not self.check_perm(request):
            return Response(status=status.HTTP_403_FORBIDDEN)
        try:
            found = logic.ingest_arp(request.data['device'], request.data['entries'])
        except (KeyError, ValueError) as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(found)


class MacIngestView(_CollectorView):
    def post(self, request):
        if not self.check_perm(request):
            return Response(status=status.HTTP_403_FORBIDDEN)
        logic.ingest_mac(request.data['device'], request.data['entries'])
        return Response({'ok': len(request.data['entries']), 'ports': logic.check_ports()})


class LocateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, ip):
        return Response(logic.locate(ip) or {})
